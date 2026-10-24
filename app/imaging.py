"""
Image helpers that need no third-party dependency.
==================================================

The product only needs a few things from an uploaded chart: validate it,
know its format and size, and derive a stable fingerprint so DEMO mode is
deterministic (same chart -> same answer). Real decoding is left to the
vision model, which receives the original bytes.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import struct

MAX_BYTES = 8 * 1024 * 1024  # 8 MB, plenty for a chart screenshot


class ImageError(ValueError):
    pass


def parse_data_url(data_url: str) -> tuple[str, bytes]:
    """Accept a data: URL or a bare base64 string; return (mime, raw_bytes)."""
    if not data_url or not isinstance(data_url, str):
        raise ImageError("empty image")
    mime = "image/jpeg"
    payload = data_url
    if data_url.startswith("data:"):
        head, _, payload = data_url.partition(",")
        if not _:
            raise ImageError("malformed data URL")
        if ";" in head:
            mime = head[5:].split(";")[0] or mime
    payload = payload.strip().replace("\n", "").replace("\r", "")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        # Browsers sometimes drop the '=' padding; retry once with it restored
        # before giving up, so a truncated-looking URL still works.
        padded = payload + "=" * (-len(payload) % 4)
        try:
            raw = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageError(f"invalid base64: {exc}") from exc
    if not raw:
        raise ImageError("empty image")
    if len(raw) > MAX_BYTES:
        raise ImageError("image too large (max 8 MB)")
    return validate_image(raw, mime), raw


# Magic bytes for the formats the vision models accept.
_MAGIC = (
    ("image/jpeg", lambda r: r[:3] == b"\xff\xd8\xff"),
    ("image/png", lambda r: r[:8] == b"\x89PNG\r\n\x1a\n"),
    ("image/gif", lambda r: r[:6] in (b"GIF87a", b"GIF89a")),
    ("image/webp", lambda r: r[:4] == b"RIFF" and r[8:12] == b"WEBP"),
)


def detect_mime(raw: bytes, fallback: str = "") -> str:
    """The image type from the bytes themselves, or "" if it is not an image.

    Never trust the declared type or the file extension: a payload is only an
    image if its magic bytes say so.
    """
    for mime, test in _MAGIC:
        if test(raw):
            return mime
    return ""


def validate_image(raw: bytes, declared: str = "") -> str:
    """Return the real mime, or raise ImageError for anything that is not one.

    This is the gate that stops a non-image (or a corrupted blob) from being
    forwarded to the paid vision model -- which would waste the user's credit
    and the provider quota on a request that cannot succeed.
    """
    mime = detect_mime(raw, declared)
    if not mime:
        raise ImageError("not a supported image (jpeg/png/gif/webp expected)")
    return mime


def _sniff_mime(raw: bytes, fallback: str) -> str:
    """Backwards-compatible wrapper: real type, else the declared fallback."""
    return detect_mime(raw, fallback) or fallback


def dimensions(raw: bytes) -> tuple[int | None, int | None]:
    """Width/height for PNG and JPEG, else (None, None)."""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", raw[16:24])
            return w, h
        if raw[:3] == b"\xff\xd8\xff":
            i, n = 2, len(raw)
            while i < n - 9:
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    return w, h
                seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
                i += 2 + seg
    except Exception:  # noqa: BLE001 - best-effort metadata only
        pass
    return None, None


def fingerprint(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def describe(raw: bytes, mime: str) -> dict:
    w, h = dimensions(raw)
    return {
        "mime": mime,
        "bytes": len(raw),
        "width": w,
        "height": h,
        "fingerprint": fingerprint(raw),
    }
