"""Image intake: validation, format sniffing and the deterministic fingerprint."""
from __future__ import annotations

import base64
import struct
import zlib

import pytest


def png(w: int = 8, h: int = 6) -> bytes:
    """A minimal but valid PNG, built by hand so tests need no Pillow."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + bytes([200, 100, 50]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def test_parse_data_url_roundtrip(mods):
    raw = png()
    url = "data:image/png;base64," + base64.b64encode(raw).decode()
    mime, out = mods["imaging"].parse_data_url(url)
    assert mime == "image/png"
    assert out == raw


def test_parse_bare_base64(mods):
    raw = png()
    mime, out = mods["imaging"].parse_data_url(base64.b64encode(raw).decode())
    assert mime == "image/png"  # sniffed from the bytes, not the absent header
    assert out == raw


def test_parse_rejects_empty(mods):
    with pytest.raises(mods["imaging"].ImageError):
        mods["imaging"].parse_data_url("")


def test_parse_rejects_oversize(mods):
    big = "A" * (11 * 1024 * 1024)  # decodes to ~8.25 MB
    with pytest.raises(mods["imaging"].ImageError):
        mods["imaging"].parse_data_url(big)


def test_parse_rejects_non_base64(mods):
    # `validate=False` used to silently drop the bad characters, so junk came
    # back as a bogus image and was forwarded to the paid model.
    with pytest.raises(mods["imaging"].ImageError):
        mods["imaging"].parse_data_url("data:image/png;base64,NOT-VALID-BASE64!!!")


def test_parse_rejects_non_image_bytes(mods):
    # Valid base64 that is not an image must be rejected, not passed through.
    junk = base64.b64encode(b"this is a text file, not a chart").decode()
    with pytest.raises(mods["imaging"].ImageError):
        mods["imaging"].parse_data_url("data:image/png;base64," + junk)


def test_parse_accepts_unpadded_base64(mods):
    raw = png()
    url = "data:image/png;base64," + base64.b64encode(raw).decode().rstrip("=")
    mime, out = mods["imaging"].parse_data_url(url)
    assert mime == "image/png" and out == raw


def test_validate_image_returns_real_mime_over_declared(mods):
    # A JPEG declared as PNG is still recognised as JPEG.
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    assert mods["imaging"].validate_image(jpeg, "image/png") == "image/jpeg"


def test_validate_image_rejects_non_image(mods):
    with pytest.raises(mods["imaging"].ImageError):
        mods["imaging"].validate_image(b"<html>hi</html>", "image/png")


def test_sniff_mime_by_magic_bytes(mods):
    sniff = mods["imaging"]._sniff_mime
    assert sniff(b"\xff\xd8\xff\xe0" + b"\x00" * 10, "image/jpeg") == "image/jpeg"
    assert sniff(png(), "image/jpeg") == "image/png"
    assert sniff(b"GIF89a" + b"\x00" * 10, "image/jpeg") == "image/gif"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBP", "image/jpeg") == "image/webp"


def test_sniff_keeps_fallback_for_unknown(mods):
    assert mods["imaging"]._sniff_mime(b"\x00\x01\x02", "image/tiff") == "image/tiff"


def test_dimensions_png(mods):
    assert mods["imaging"].dimensions(png(20, 13)) == (20, 13)


def test_dimensions_unknown_is_none(mods):
    assert mods["imaging"].dimensions(b"\x00\x01\x02\x03") == (None, None)


def test_fingerprint_is_stable_and_distinct(mods):
    fp = mods["imaging"].fingerprint
    assert fp(png()) == fp(png())
    assert fp(png(8, 6)) != fp(png(9, 6))


def test_describe_shape(mods):
    d = mods["imaging"].describe(png(10, 10), "image/png")
    assert d["mime"] == "image/png"
    assert d["width"] == 10 and d["height"] == 10
    assert d["bytes"] > 0 and len(d["fingerprint"]) == 64
