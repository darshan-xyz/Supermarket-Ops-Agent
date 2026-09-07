"""One Telegram message → one agent turn powered by Google Gemini API.

Maps a Telegram chat to a persisted Gemini conversation history (resume across messages),
sets the `current_chat` ContextVar so tools know which chat they serve,
captures the final reply and any files the document tools queued,
and persists the session state in SQLite so context survives process restarts.

`/new` clears the session history but never the store DB — stock, khata, and preferences
persist across sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

from google.genai import Client, types

from ..config import FALLBACK_MODELS, GEMINI_API_KEY, MAX_TURNS
from ..tools.context import OutFile, current_chat, current_user_message, get_repo, take_output_files
from ..tools.registry import TOOLS_BY_NAME, build_gemini_tools
from .guardrails import get_out_of_scope_reply, is_out_of_scope_query
from .system_prompt import build_system_prompt

log = logging.getLogger("kirana.session")
_audit_log = logging.getLogger("kirana.audit")

_client: Client | None = None


def get_client() -> Client:
    """Return the memoized Google GenAI Client."""
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set in environment or .env.")
        _client = Client(api_key=GEMINI_API_KEY)
    return _client


@dataclass
class TurnResult:
    reply: str
    files: list[OutFile] = field(default_factory=list)


def _load_chat_history(repo, chat_id: int) -> list[types.Content]:
    """Load serialized conversation history for a chat_id from SQLite."""
    raw = repo.get_session(chat_id)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            contents = [types.Content.model_validate(c) for c in data]
            # Sanitize: ensure loaded history only contains clean text dialogue
            cleaned = []
            for c in contents:
                text_parts = [p for p in c.parts if p.text and not p.function_call and not p.function_response]
                if text_parts:
                    cleaned.append(types.Content(role=c.role, parts=text_parts))
            return cleaned
    except Exception as e:
        log.warning("Failed to deserialize chat session for chat_id=%s: %s", chat_id, e)
    return []


def _save_chat_history(repo, chat_id: int, history: list[types.Content]) -> None:
    """Save conversation history for a chat_id to SQLite."""
    try:
        # Keep last 20 messages to preserve ample conversational context
        trimmed = history[-20:]
        raw = json.dumps([c.to_json_dict() for c in trimmed], ensure_ascii=False)
        repo.set_session(chat_id, raw)
    except Exception as e:
        log.warning("Failed to save chat session for chat_id=%s: %s", chat_id, e)


async def _generate_content_with_fallback(
    client: Client,
    contents: list[types.Content],
    config: types.GenerateContentConfig,
    preferred_model: str | None = None,
) -> tuple[types.GenerateContentResponse, str]:
    """Call generate_content with automatic fallback across models if 429/503 occurs."""
    order: list[str] = []
    if preferred_model:
        order.append(preferred_model)
    for m in FALLBACK_MODELS:
        if m not in order:
            order.append(m)

    last_exc: Exception | None = None
    for m in order:
        for attempt in range(2):
            try:
                resp = await client.aio.models.generate_content(
                    model=m,
                    contents=contents,
                    config=config,
                )
                return resp, m
            except Exception as exc:
                last_exc = exc
                err = str(exc)
                if ("429" in err or "RESOURCE_EXHAUSTED" in err) or ("503" in err or "UNAVAILABLE" in err):
                    log.warning(
                        "Gemini model %s hit rate/capacity limit (attempt %d/2): %s. Trying fallback...",
                        m,
                        attempt + 1,
                        "429" if "429" in err else "503",
                    )
                    await asyncio.sleep(1.0)
                    if attempt == 0 and "503" in err:
                        continue
                    break
                elif "404" in err or "NOT_FOUND" in err:
                    break
                else:
                    raise exc

    if last_exc:
        raise last_exc
    raise RuntimeError("No Gemini models available.")


async def run_turn(
    chat_id: int,
    text: str,
    media_bytes: bytes | None = None,
    media_mime: str | None = None,
) -> TurnResult:
    """Run an agent turn with Gemini, handling multimodal input (text, voice, photo) and tool calls."""
    # Set chat context for tools FIRST so get_repo() resolves the tenant repo
    token = current_chat.set(chat_id)
    msg_token = current_user_message.set(text or "")
    try:
        repo = get_repo()

        # Pre-turn Guardrail: Intercept explicit out-of-scope queries (e.g. coding, trivia, prompt injections)
        if text and not (media_bytes and media_mime):
            is_out_scope, detected_lang = is_out_of_scope_query(text)
            if is_out_scope:
                store_lang = repo.get_preference("language") or detected_lang
                reply = get_out_of_scope_reply(store_lang)
                history = _load_chat_history(repo, chat_id)
                history.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
                history.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
                _save_chat_history(repo, chat_id, history)
                return TurnResult(reply=reply, files=[])

        client = get_client()

        # Load persisted dialogue history
        history = _load_chat_history(repo, chat_id)
        tools = build_gemini_tools()
        sys_prompt = build_system_prompt(repo, chat_id=chat_id)

        config = types.GenerateContentConfig(
            system_instruction=sys_prompt,
            tools=tools,
            temperature=0.2,
        )

        # Build parts: text + optional voice/photo media
        parts: list[types.Part] = []
        if media_bytes and media_mime:
            parts.append(types.Part.from_bytes(data=media_bytes, mime_type=media_mime))
        if text:
            parts.append(types.Part.from_text(text=text))
        elif not parts:
            parts.append(types.Part.from_text(text=""))

        # Working contents for current turn's tool-calling loop
        current_contents = list(history)
        current_contents.append(
            types.Content(
                role="user",
                parts=parts,
            )
        )

        final_reply: str = ""
        turn_count = 0
        reply_text_parts: list[str] = []
        active_model: str | None = None

        while turn_count < MAX_TURNS:
            turn_count += 1
            response, active_model = await _generate_content_with_fallback(
                client=client,
                contents=current_contents,
                config=config,
                preferred_model=active_model,
            )

            # Check if model produced function calls or text
            candidate = response.candidates[0] if response.candidates else None
            if not candidate or not candidate.content:
                final_reply = response.text or "(no reply)"
                break

            model_content = candidate.content
            current_contents.append(model_content)

            # Extract any function calls
            function_calls: list[types.FunctionCall] = []
            reply_text_parts = []

            for part in model_content.parts:
                if part.function_call:
                    function_calls.append(part.function_call)
                if part.text:
                    reply_text_parts.append(part.text)

            if not function_calls:
                # No more tools called, turn is complete!
                final_reply = "".join(reply_text_parts).strip()
                break

            # Execute all function calls requested by the model
            response_parts: list[types.Part] = []
            for fc in function_calls:
                fn_name = fc.name
                fn_args = dict(fc.args) if fc.args else {}

                # Audit logging
                compact_args = json.dumps(fn_args, ensure_ascii=False, default=str)
                _audit_log.info("tool=%s input=%s", fn_name, compact_args)

                # Resolve tool (supports direct name or mcp__kirana__ prefixed name)
                clean_name = fn_name.replace("mcp__kirana__", "")
                tool_obj = TOOLS_BY_NAME.get(clean_name) or TOOLS_BY_NAME.get(fn_name)
                if tool_obj:
                    try:
                        res = await tool_obj.handler(fn_args)
                    except Exception as exc:  # noqa: BLE001
                        res = {
                            "content": [{"type": "text", "text": f"[internal_error] {exc}"}],
                            "is_error": True,
                        }
                else:
                    res = {
                        "content": [{"type": "text", "text": f"Tool '{fn_name}' not found."}],
                        "is_error": True,
                    }

                response_parts.append(
                    types.Part.from_function_response(
                        name=fn_name,
                        response=res,
                    )
                )

            # Append the tool responses as a user turn
            current_contents.append(
                types.Content(
                    role="user",
                    parts=response_parts,
                )
            )
        else:
            final_reply = "".join(reply_text_parts).strip() or "Completed the requested actions."

        reply = final_reply.strip() or "(no reply)"

        # Persist dialogue history in SQLite (prevents orphaned function turns across sessions)
        history.append(types.Content(role="user", parts=[types.Part.from_text(text=text or "(voice/photo order)")]))
        history.append(types.Content(role="model", parts=[types.Part.from_text(text=reply)]))
        _save_chat_history(repo, chat_id, history)

        # Collect any artifacts (PDF invoices, PPTX decks) queued during this turn
        files = take_output_files(chat_id)
        return TurnResult(reply=reply, files=files)
    finally:
        current_user_message.reset(msg_token)
        current_chat.reset(token)


def reset_session(chat_id: int) -> None:
    """Drop the conversation session; durable store state is untouched."""
    token = current_chat.set(chat_id)
    try:
        get_repo().clear_session(chat_id)
    finally:
        current_chat.reset(token)

