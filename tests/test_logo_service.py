"""Tests for production-grade store logo validation, format sniffing,
sanitization, and PDF invoice rendering.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image


from kirana.artifacts.invoice_pdf import render_invoice
from kirana.artifacts.sales_report_pdf import render_sales_report_pdf
from kirana.services.logo_service import (
    MAX_LOGO_SIZE_BYTES,
    sniff_file_type,
    validate_and_process_logo,
)


def _make_test_image_bytes(fmt: str = "PNG", size: tuple[int, int] = (200, 200), color: str = "green") -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGBA" if fmt == "PNG" else "RGB", size, color=color)
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_sniff_file_type():
    # PDF
    assert sniff_file_type(b"%PDF-1.4 header...") == "PDF document"
    # PNG
    assert sniff_file_type(b"\x89PNG\r\n\x1a\n\x00\x00") == "PNG image"
    # JPEG
    assert sniff_file_type(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "JPEG image"
    # WEBP
    assert sniff_file_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "WEBP image"
    # Audio
    assert sniff_file_type(b"ID3\x03\x00\x00\x00") == "MP3 audio file"
    assert sniff_file_type(b"OggS\x00\x02\x00\x00") == "OGG audio/voice file"
    # Zip / Office
    assert sniff_file_type(b"PK\x03\x04\x14\x00\x00\x00") == "ZIP archive / Office document"
    # Empty
    assert sniff_file_type(b"") == "Empty file"


def test_valid_png_logo(tmp_path: Path):
    raw = _make_test_image_bytes("PNG", (150, 150))
    dest = tmp_path / "test_logo.png"

    res = validate_and_process_logo(raw, dest)
    assert res.is_valid is True
    assert res.saved_path == str(dest.resolve())
    assert dest.exists()
    assert res.width == 150
    assert res.height == 150


def test_valid_jpeg_and_webp_logo(tmp_path: Path):
    # JPEG converted to PNG
    raw_jpg = _make_test_image_bytes("JPEG", (300, 150))
    dest_jpg = tmp_path / "from_jpg.png"
    res1 = validate_and_process_logo(raw_jpg, dest_jpg)
    assert res1.is_valid is True
    assert dest_jpg.exists()

    # Verify output is a valid PNG
    with Image.open(dest_jpg) as img:
        assert img.format == "PNG"
        assert img.size == (300, 150)

    # WEBP converted to PNG
    raw_webp = _make_test_image_bytes("WEBP", (100, 100))
    dest_webp = tmp_path / "from_webp.png"
    res2 = validate_and_process_logo(raw_webp, dest_webp)
    assert res2.is_valid is True
    assert dest_webp.exists()


def test_large_image_retina_downsampled(tmp_path: Path):
    # 2000 x 1000 image downsampled to <= 800 x 800
    raw_big = _make_test_image_bytes("PNG", (2000, 1000))
    dest = tmp_path / "downsampled.png"
    res = validate_and_process_logo(raw_big, dest)
    assert res.is_valid is True
    assert res.width == 800
    assert res.height == 400


def test_reject_pdf_file(tmp_path: Path):
    fake_pdf = b"%PDF-1.5\n%trailer\n%%EOF"
    dest = tmp_path / "logo.png"

    res = validate_and_process_logo(fake_pdf, dest)
    assert res.is_valid is False
    assert res.error_code == "invalid_file_type"
    assert res.detected_type == "PDF document"
    assert "PDF document" in res.get_message("en")
    assert "PDF document" in res.get_message("ta")
    assert not dest.exists()


def test_reject_audio_and_zip(tmp_path: Path):
    fake_mp3 = b"ID3\x03\x00\x00\x00\x00\x00#TALB\x00\x00\x00"
    dest1 = tmp_path / "logo1.png"
    res1 = validate_and_process_logo(fake_mp3, dest1)
    assert res1.is_valid is False
    assert res1.detected_type == "MP3 audio file"

    fake_zip = b"PK\x03\x04\x14\x00\x00\x00\x08\x00"
    dest2 = tmp_path / "logo2.png"
    res2 = validate_and_process_logo(fake_zip, dest2)
    assert res2.is_valid is False
    assert res2.detected_type == "ZIP archive / Office document"


def test_reject_corrupt_file(tmp_path: Path):
    # Valid PNG magic header but corrupt stream afterwards
    corrupt_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 30
    dest = tmp_path / "corrupt.png"
    res = validate_and_process_logo(corrupt_png, dest)
    assert res.is_valid is False
    assert res.error_code == "corrupted_image"


def test_reject_oversized_file(tmp_path: Path):
    huge_data = b"\x89PNG\r\n\x1a\n" + b"X" * (MAX_LOGO_SIZE_BYTES + 100)
    dest = tmp_path / "huge.png"
    res = validate_and_process_logo(huge_data, dest)
    assert res.is_valid is False
    assert res.error_code == "file_too_large"


def test_logo_rendering_in_invoice_and_report_pdf(repo, tmp_path: Path):
    # 1. Create a dummy logo file
    logo_file = tmp_path / "store_logo.png"
    Image.new("RGBA", (120, 120), color="blue").save(logo_file)

    # 2. Set shop profile with logo_path
    repo.set_shop_profile(name="Krishna Supermarket", logo_path=str(logo_file))
    prof = repo.get_shop_profile()
    assert prof.logo_path == str(logo_file)

    # 3. Create a bill and finalize it
    p = repo.add_product(name="Maggi 70g", unit="packet", gst_rate=18,
                         mrp_paise=1400, sell_paise=1400, cost_paise=1200,
                         opening_stock_base=20 * 1000)
    repo.cart_get_or_create(100)
    repo.cart_add_item(100, p.id, 2)
    repo.cart_set_payment(100, "cash")


    fin = repo.finalize_bill(100, idempotency_key="LOGO-TEST-KEY-1")

    # 4. Render invoice PDF with logo
    pdf_path = render_invoice(repo, fin.id, out_dir=str(tmp_path))
    assert Path(pdf_path).exists()
    assert Path(pdf_path).stat().st_size > 1000

    # 5. Render daily sales report PDF with logo
    today = fin.finalized_at[:10]
    rep_path = render_sales_report_pdf(repo, today, today, out_dir=str(tmp_path))

    assert Path(rep_path).exists()
    assert Path(rep_path).stat().st_size > 1000
