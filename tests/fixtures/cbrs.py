"""CBR generation helper for tests.

Writes a minimal RAR 4 archive with every file *stored* (method 0x30, no
compression), which is all a CBR-read test needs — and unlike `rar a`, needs no
non-free binary, so the fixture can be built on the fly in any environment.

Reading it back still requires one of rarfile's external backends (unar or
unrar) on PATH; rarfile is only a wrapper around those.

Layout per the RAR 4 block format:

    marker block        Rar!\\x1a\\x07\\x00
    main header         type 0x73, 13 bytes
    file header + data  type 0x74, 32 bytes + name, then PACK_SIZE bytes
    ...

Each block's HEAD_CRC is the low 16 bits of the CRC32 over the block from
HEAD_TYPE onwards (i.e. everything after the HEAD_CRC field itself).
"""
import io
import struct
from pathlib import Path
from zlib import crc32

from PIL import Image

_MARKER = b"Rar!\x1a\x07\x00"

_MAIN_HEAD = 0x73
_FILE_HEAD = 0x74

_LONG_BLOCK = 0x8000  # ADD_SIZE field present (packed data follows the header)
_METHOD_STORED = 0x30
_HOST_OS_UNIX = 3
_UNP_VER = 20  # decoder version 2.0 — enough for stored files
_FILE_HEAD_SIZE = 32  # fixed part, before the filename

# 1980-01-01 00:00:00 in MS-DOS packed format: the epoch of the DOS timestamp,
# so fixtures are byte-identical across runs.
_DOS_TIME = 0x00210000


def _block(head_type: int, flags: int, tail: bytes, add_size: bytes = b"") -> bytes:
    """Assemble one RAR block, filling in HEAD_SIZE and HEAD_CRC."""
    head_size = 2 + 1 + 2 + 2 + len(tail)  # crc + type + flags + size + tail
    body = struct.pack("<BHH", head_type, flags, head_size) + tail
    return struct.pack("<H", crc32(body) & 0xFFFF) + body + add_size


def _file_block(name: str, data: bytes) -> bytes:
    encoded = name.encode("ascii")
    tail = struct.pack(
        "<IIBIIBBHI",
        len(data),          # PACK_SIZE — stored, so same as unpacked
        len(data),          # UNP_SIZE
        _HOST_OS_UNIX,      # HOST_OS
        crc32(data),        # FILE_CRC
        _DOS_TIME,          # FTIME
        _UNP_VER,           # UNP_VER
        _METHOD_STORED,     # METHOD
        len(encoded),       # NAME_SIZE
        0o644,              # ATTR
    ) + encoded
    assert 2 + 1 + 2 + 2 + len(tail) == _FILE_HEAD_SIZE + len(encoded)
    return _block(_FILE_HEAD, _LONG_BLOCK, tail, add_size=data)


def make_cbr(path: Path, num_pages: int = 2) -> Path:
    """Write a RAR 4 archive of `num_pages` tiny PNGs to `path`."""
    out = bytearray(_MARKER)
    out += _block(_MAIN_HEAD, 0x0000, struct.pack("<HI", 0, 0))  # HighPosAV, PosAV
    for i in range(num_pages):
        img = Image.new("RGB", (100, 150), color=(i * 40, i * 40, i * 40))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        out += _file_block(f"page{i + 1:03d}.png", buf.getvalue())
    path.write_bytes(bytes(out))
    return path
