"""Minimal, dependency-free pixel-dimension readers.

`photos_download` and `mt5_screenshot` both need to report the dimensions of
a file they just downloaded, and the project has no image library dependency
(Pillow isn't in pyproject.toml, and the brief prefers the standard library
where it reaches). PNG dimensions are a fixed byte offset - trivial. JPEG
needs a short walk of its marker segments. Both return `None` rather than
raising on anything unrecognised: dimensions are a nice-to-have on the tool
result, never worth failing the whole download over.
"""

import struct


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    # IHDR is always the first chunk, immediately after the signature:
    # 4 bytes length, 4 bytes "IHDR", 4 bytes width, 4 bytes height.
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    pos = 2
    length = len(data)
    while pos + 4 <= length:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        # SOF0-SOF15 (excluding DHT/JPG/DAC, which aren't frame markers)
        # carry the image dimensions. Every other marker is skipped over
        # using its own declared segment length.
        is_sof = 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC)
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if pos + 4 > length:
            return None
        seg_len = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        if is_sof:
            if pos + 9 > length:
                return None
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return width, height
        if marker == 0xDA:  # start of scan - no more markers of interest
            return None
        pos += 2 + seg_len
    return None


def dimensions(data: bytes, content_type: str) -> tuple[int, int] | None:
    """Best-effort dispatch by content type. Returns None for formats this
    module doesn't parse (e.g. TIFF originals) rather than guessing."""
    if "png" in content_type:
        return png_dimensions(data)
    if "jpeg" in content_type or "jpg" in content_type:
        return jpeg_dimensions(data)
    return None
