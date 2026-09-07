"""Production-grade Store Logo Processing, Validation, and Normalization Service.

Provides deep magic-byte inspection, format verification, size limits,
EXIF orientation correction, RGBA transparency preservation, retina downsampling,
and localized rejection feedback for Indian Supermarket & Kirana operations.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps

logger = logging.getLogger("kirana.logo_service")

# Max allowed upload size: 5 MB
MAX_LOGO_SIZE_BYTES = 5 * 1024 * 1024

# Minimum and maximum logo resolution
MIN_LOGO_DIMENSION = 32
MAX_LOGO_DIMENSION = 800

# Supported image formats for invoice embedding
SUPPORTED_IMAGE_FORMATS = {"PNG", "JPEG", "WEBP", "MPO"}


@dataclass
class LogoValidationResult:
    is_valid: bool
    error_code: str | None = None
    error_message: dict[str, str] | None = None
    detected_type: str | None = None
    saved_path: str | None = None
    width: int = 0
    height: int = 0

    def get_message(self, lang: str = "en") -> str:
        if not self.error_message:
            return ""
        from ..telegram.onboarding import normalize_lang
        key = normalize_lang(lang)
        return self.error_message.get(key, self.error_message.get("en", ""))


def sniff_file_type(header: bytes) -> str:
    """Sniff the actual file format from the initial magic bytes."""
    if not header:
        return "Empty file"

    # PDF
    if header.startswith(b"%PDF-"):
        return "PDF document"

    # Images
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG image"
    if header.startswith(b"\xff\xd8\xff"):
        return "JPEG image"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "WEBP image"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "GIF animation"
    if header.startswith(b"BM"):
        return "BMP image"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF image"

    # Audio & Video
    if header.startswith(b"ID3") or header.startswith(b"\xff\xfb") or header.startswith(b"\xff\xf3"):
        return "MP3 audio file"
    if header.startswith(b"OggS"):
        return "OGG audio/voice file"
    if len(header) >= 8 and header[4:8] in (b"ftyp", b"moov"):
        return "MP4 / Video file"

    # Archives & Documents
    if header.startswith(b"PK\x03\x04"):
        return "ZIP archive / Office document"
    if header.startswith(b"Rar!\x1a\x07"):
        return "RAR archive"
    if header.startswith(b"{\\rtf"):
        return "RTF document"

    # Plain text check
    try:
        header.decode("utf-8")
        return "Text file / Document"
    except UnicodeDecodeError:
        pass

    return "Unsupported binary file"


def validate_and_process_logo(
    data: bytes | BinaryIO,
    dest_path: Path | str,
) -> LogoValidationResult:
    """Validate, sanitize, downsample, and save a store logo image.

    Performs:
      1. Size checks (<= 5MB).
      2. Magic-byte sniffing to detect non-images (PDF, audio, zip, etc.).
      3. Pillow format verification and decoding integrity check.
      4. Auto-rotation via EXIF orientation tags.
      5. Color space normalization (CMYK -> RGB; preserves transparency for RGBA).
      6. Proportional downsampling to <= 800x800 px with Lanczos resampling.
      7. Writes out a clean, standardized PNG file to dest_path.
    """
    dest = Path(dest_path)

    # 1. Read bytes & check size
    if isinstance(data, (bytes, bytearray)):
        raw_bytes = bytes(data)
    else:
        raw_bytes = data.read()

    file_size = len(raw_bytes)
    if file_size == 0:
        return LogoValidationResult(
            is_valid=False,
            error_code="empty_file",
            error_message={
                "en": "⚠️ The uploaded file is empty. Please send a valid image file.",
                "ta": "⚠️ பதிவேற்றப்பட்ட கோப்பு காலியாக உள்ளது. தயவுசெய்து சரியான படக் கோப்பை அனுப்பவும்.",
                "hi": "⚠️ अपलोड की गई फ़ाइल खाली है। कृपया एक मान्य इमेज फ़ाइल भेजें।",
            },
        )

    if file_size > MAX_LOGO_SIZE_BYTES:
        size_mb = file_size / (1024 * 1024)
        return LogoValidationResult(
            is_valid=False,
            error_code="file_too_large",
            error_message={
                "en": f"⚠️ The file is too large ({size_mb:.1f} MB). Logos must be under 5 MB. Please upload a smaller image file.",
                "ta": f"⚠️ கோப்பின் அளவு மிக அதிகம் ({size_mb:.1f} MB). லோகோ 5 MB-க்குள் இருக்க வேண்டும். தயவுசெய்து சிறிய படக் கோப்பை அனுப்பவும்.",
                "hi": f"⚠️ फ़ाइल का आकार बहुत बड़ा है ({size_mb:.1f} MB)। लोगो 5 MB से कम होना चाहिए। कृपया छोटी इमेज फ़ाइल भेजें।",
            },
        )

    # 2. Magic byte sniffing
    detected_type = sniff_file_type(raw_bytes[:64])
    is_supported_image = detected_type in ("PNG image", "JPEG image", "WEBP image")

    if not is_supported_image:
        return LogoValidationResult(
            is_valid=False,
            error_code="invalid_file_type",
            detected_type=detected_type,
            error_message={
                "en": (
                    f"⚠️ <b>Invalid file format:</b> We received a {detected_type}.\n\n"
                    "Store logos must be an image file (<b>PNG, JPG, JPEG, or WEBP</b>).\n\n"
                    "👉 Uploading a store logo image is mandatory to proceed with store setup. Please upload a valid image file."
                ),
                "ta": (
                    f"⚠️ <b>தவறான கோப்பு வடிவம்:</b> நீங்கள் {detected_type} அனுப்பியுள்ளீர்கள்.\n\n"
                    "கடையின் லோகோ பட வடிவில் (<b>PNG, JPG, JPEG அல்லது WEBP</b>) இருக்க வேண்டும்.\n\n"
                    "👉 கடை அமைப்பை முடிக்க லோகோ பதிவேற்றம் கட்டாயமாகும். தயவுசெய்து சரியான படக் கோப்பை அனுப்பவும்."
                ),
                "hi": (
                    f"⚠️ <b>अमान्य फ़ाइल प्रकार:</b> आपने {detected_type} भेजा है।\n\n"
                    "स्टोर का लोगो इमेज फ़ाइल (<b>PNG, JPG, JPEG या WEBP</b>) होना चाहिए।\n\n"
                    "👉 स्टोर सेटअप पूरा करने के लिए लोगो अपलोड अनिवार्य है। कृपया मान्य इमेज फ़ाइल भेजें।"
                ),
            },
        )


    # 3. Pillow open & verify
    try:
        stream = io.BytesIO(raw_bytes)
        img = Image.open(stream)
        img.verify()  # Verifies integrity without decoding entire raster
    except Exception as exc:
        logger.warning("Pillow verification failed: %s", exc)
        return LogoValidationResult(
            is_valid=False,
            error_code="corrupted_image",
            detected_type=detected_type,
            error_message={
                "en": "⚠️ The image file appears damaged or corrupted. Please upload a clear photo or image.",
                "ta": "⚠️ படக் கோப்பு சிதைந்துள்ளது அல்லது சேதமடைந்துள்ளது. தயவுசெய்து தெளிவான படத்தை மீண்டும் அனுப்பவும்.",
                "hi": "⚠️ इमेज फ़ाइल क्षतिग्रस्त या अमान्य लग रही है। कृपया एक साफ़ फ़ोटो या इमेज पुनः भेजें।",
            },
        )

    # Re-open stream for actual processing after verify()
    try:
        stream = io.BytesIO(raw_bytes)
        img = Image.open(stream)

        # 4. EXIF Orientation correction
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass

        orig_w, orig_h = img.size
        if orig_w < MIN_LOGO_DIMENSION or orig_h < MIN_LOGO_DIMENSION:
            return LogoValidationResult(
                is_valid=False,
                error_code="image_too_small",
                error_message={
                    "en": f"⚠️ Image is too small ({orig_w}x{orig_h} px). Please provide a logo at least 32x32 px.",
                    "ta": f"⚠️ படத்தின் அளவு மிகச் சிறியது ({orig_w}x{orig_h} px). குறைந்தது 32x32 px அளவில் லோகோவை அனுப்பவும்.",
                    "hi": f"⚠️ इमेज बहुत छोटी है ({orig_w}x{orig_h} px)। कृपया कम से कम 32x32 px का लोगो प्रदान करें।",
                },
            )

        # 5. Color space conversion
        # Preserve transparency if available
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            img = img.convert("RGBA")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 6. Proportional retina downsampling if larger than max dimensions
        if orig_w > MAX_LOGO_DIMENSION or orig_h > MAX_LOGO_DIMENSION:
            img.thumbnail((MAX_LOGO_DIMENSION, MAX_LOGO_DIMENSION), Image.Resampling.LANCZOS)

        final_w, final_h = img.size

        # 7. Standardize & save as PNG
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(dest), format="PNG", optimize=True)

        logger.info("Successfully validated and normalized logo to %s (%dx%d)", dest, final_w, final_h)
        return LogoValidationResult(
            is_valid=True,
            saved_path=str(dest.resolve()),
            detected_type=detected_type,
            width=final_w,
            height=final_h,
        )

    except Exception as exc:
        logger.error("Failed to process image: %s", exc, exc_info=True)
        return LogoValidationResult(
            is_valid=False,
            error_code="processing_error",
            error_message={
                "en": "⚠️ Could not process this image format. Please upload a standard PNG or JPG file.",
                "ta": "⚠️ இந்தப் பட வடிவமைப்பைச் செயலாக்க முடியவில்லை. தயவுசெய்து வழக்கமான PNG அல்லது JPG கோப்பை அனுப்பவும்.",
                "hi": "⚠️ इस इमेज फॉर्मेट को प्रोसेस नहीं किया जा सका। कृपया एक सामान्य PNG या JPG फ़ाइल भेजें।",
            },
        )
