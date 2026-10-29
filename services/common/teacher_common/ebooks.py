"""MOBI / AZW / AZW3 (KF8) conversion.

Kindle files are unpacked with the pure-Python ``mobi`` package (a KindleUnpack port), so the ingestion
image needs no Calibre. KF8 books unpack to an EPUB that the normal EPUB parser reads; old MOBI 6 books
unpack to one HTML file. DRM-protected books are detected from the file header before anything is unpacked
and refused with a clear reason. The source file is only read, never written.
"""

import logging
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("teacher.ingestion")

EBOOK_EXTENSIONS = {".mobi", ".azw", ".azw3"}

# PalmDB header: 32 byte name, ... type/creator at 60..68, record count at 76, record list from 78.
_PALMDB_HEADER = 78
_MOBI_MAGIC = b"BOOKMOBI"
_PALMDOC_MAGIC = b"TEXtREAd"


class DrmProtectedError(ValueError):
    """The book is encrypted; it is skipped, never decrypted."""


@dataclass
class MobiInfo:
    encryption: int
    version: int | None
    codepage: int | None


def read_mobi_header(path: Path) -> MobiInfo:
    """Read the bits of the MOBI header needed to refuse unreadable books; raises ValueError if it is not a MOBI."""
    with path.open("rb") as handle:
        head = handle.read(_PALMDB_HEADER + 8)
        if head[:4] in (b"PK\x03\x04", b"CONT") or head[:3] == b"TPZ":
            raise ValueError(
                f"{path.name} is a Kindle KFX/Topaz container, which cannot be converted. "
                "Re-download it as MOBI/AZW3 or convert it to EPUB."
            )
        if len(head) < _PALMDB_HEADER + 8 or head[60:68] not in (_MOBI_MAGIC, _PALMDOC_MAGIC):
            raise ValueError(f"{path.name} is not a valid MOBI/AZW file (no PalmDB 'BOOKMOBI' header)")
        record0_offset = struct.unpack(">L", head[_PALMDB_HEADER:_PALMDB_HEADER + 4])[0]
        handle.seek(record0_offset)
        record0 = handle.read(0x80)
    if len(record0) < 16:
        raise ValueError(f"{path.name} is truncated or corrupt (no MOBI header record)")
    encryption = struct.unpack(">H", record0[12:14])[0]
    version = codepage = None
    if record0[16:20] == b"MOBI" and len(record0) >= 0x20:
        codepage = struct.unpack(">L", record0[28:32])[0]
        version = struct.unpack(">L", record0[36:40])[0] if len(record0) >= 40 else None
    return MobiInfo(encryption=encryption, version=version, codepage=codepage)


def check_readable(path: Path) -> MobiInfo:
    info = read_mobi_header(path)
    if info.encryption:
        kind = {1: "old Mobipocket", 2: "Mobipocket/Kindle"}.get(info.encryption, f"type {info.encryption}")
        raise DrmProtectedError(
            f"{path.name} is DRM-protected ({kind} encryption); skipped. "
            "Only DRM-free books can be indexed."
        )
    return info


@dataclass
class ConvertedEbook:
    """Result of unpacking: an EPUB or HTML file inside ``workdir``. Call ``cleanup`` when done."""

    path: Path
    kind: str  # "epub" or "html"
    workdir: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def convert_ebook(path: Path) -> ConvertedEbook:
    """Unpack a MOBI/AZW/AZW3 into a temporary EPUB or HTML file. Raises DrmProtectedError or ValueError."""
    check_readable(path)
    try:
        from mobi import extract
    except ImportError as exc:
        raise ValueError(f"MOBI/AZW support needs the 'mobi' package, which is not installed: {exc}") from exc
    workdir: Path | None = None
    try:
        # extract() copies a file object into its temp dir, so the unpacker never touches the share.
        with path.open("rb") as handle:
            tempdir, extracted = extract(handle)
        workdir = Path(tempdir)
    except Exception as exc:  # noqa: BLE001 - the unpacker raises assorted errors on corrupt books
        raise ValueError(f"Could not convert {path.name}: {exc}") from exc
    result = Path(extracted)
    suffix = result.suffix.lower()
    if suffix == ".epub":
        return ConvertedEbook(result, "epub", workdir)
    if suffix in {".html", ".htm"}:
        return ConvertedEbook(result, "html", workdir)
    shutil.rmtree(workdir, ignore_errors=True)
    raise ValueError(f"{path.name} is a print-replica/PDF Kindle book; it has no text to extract")
