# Kirana Ops Agent — runtime image.
# Powered by Google Gemini API.

FROM python:3.12-slim

WORKDIR /app

# --- Python deps (layer-cached) ---
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# --- App: skills, scripts ---
COPY skills ./skills
COPY scripts ./scripts

# Non-root
RUN useradd -m app && mkdir -p /app/data/stores/logos /app/artifacts_out \
    && chown -R app:app /app
USER app

ENV KIRANA_PROJECT_ROOT=/app \
    KIRANA_DB_PATH=/app/data/kirana.db \
    KIRANA_MASTER_DB_PATH=/app/data/master.db \
    KIRANA_STORES_DIR=/app/data/stores \
    KIRANA_ARTIFACTS_DIR=/app/artifacts_out \
    PORT=10000 \
    PYTHONUNBUFFERED=1

EXPOSE 10000

# Seed the catalog on first run if the DB is empty, then start the bot.
CMD ["sh", "-c", "python scripts/seed.py 2>/dev/null; python -m kirana.telegram.bot"]
