"""MOBI/AZW/AZW3 conversion. Small MOBI 6 books are written by hand (no binary fixtures to keep in the repo)."""

import importlib.util
import shutil
import struct
import sys
from pathlib import Path

import pytest
from ebooklib import epub
from teacher_common import ebooks

TEXT_HU = (
    "Árvíztűrő tükörfúrógép. A magyar nyelv ragozó nyelv, amelyben a toldalékok sorrendje szigorú szabályokat követ. "
    "Öt szép szűz lány őrült írót nyúz. "
) * 4


def _load_real_documents():
    """conftest stubs teacher_common.documents; these tests need the real parser (same trick as test_ocr)."""
    stub = sys.modules.get("teacher_common.documents")
    path = Path(__file__).resolve().parents[1] / "services" / "common" / "teacher_common" / "documents.py"
    spec = importlib.util.spec_from_file_location("teacher_common.documents", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["teacher_common.documents"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if stub is not None:
            sys.modules["teacher_common.documents"] = stub
    return module


documents = _load_real_documents()


@pytest.fixture(autouse=True)
def real_documents(monkeypatch):
    monkeypatch.setitem(sys.modules, "teacher_common.documents", documents)


def make_mobi(path: Path, html: str, title: str = "Teszt könyv", encoding: str = "utf-8", encryption: int = 0) -> Path:
    """An uncompressed MOBI 6 file: PalmDB header, record 0 (PalmDOC + MOBI + EXTH + name), text records."""
    codepage = {"utf-8": 65001, "cp1252": 1252}[encoding]
    body = html.encode(encoding, errors="replace")
    records = [body[i:i + 4096] for i in range(0, len(body), 4096)] or [b""]
    name = title.encode(encoding, errors="replace")
    exth_data = struct.pack(">LL", 503, 8 + len(name)) + name  # 503 = updated title
    exth = b"EXTH" + struct.pack(">LL", 12 + len(exth_data), 1) + exth_data
    exth += b"\x00" * (-len(exth) % 4)

    mobi = bytearray(232)
    mobi[0:4] = b"MOBI"
    struct.pack_into(">L", mobi, 4, 232)  # header length
    struct.pack_into(">L", mobi, 8, 2)  # type: book
    struct.pack_into(">L", mobi, 12, codepage)
    struct.pack_into(">L", mobi, 16, 0xFFFFFFFF)
    struct.pack_into(">L", mobi, 20, 6)  # MOBI version
    for offset in (24, 28, 32, 36, 40, 44, 48, 52, 56, 60, 64):
        struct.pack_into(">L", mobi, offset, 0xFFFFFFFF)  # index records: none
    struct.pack_into(">L", mobi, 64, len(records) + 1)  # first non-book record
    name_offset = 16 + 232 + len(exth)
    struct.pack_into(">L", mobi, 68, name_offset)
    struct.pack_into(">L", mobi, 72, len(name))
    struct.pack_into(">L", mobi, 76, 0x409)
    struct.pack_into(">L", mobi, 92, len(records) + 1)  # first image record: none after the text
    struct.pack_into(">L", mobi, 112, 0x40)  # EXTH flag
    for offset in (100, 104, 108, 116, 120):
        struct.pack_into(">L", mobi, offset, 0)
    struct.pack_into(">L", mobi, 160, 0xFFFFFFFF)
    for offset in (168, 172, 176, 180, 184, 188, 192, 196, 200, 204, 208, 212, 216, 220, 224, 228):
        struct.pack_into(">L", mobi, offset, 0xFFFFFFFF)
    palmdoc = struct.pack(">HHLHHHH", 1, 0, len(body), len(records), 4096, encryption, 0)
    record0 = palmdoc + bytes(mobi) + exth + name + b"\x00" * 4
    sections = [record0, *records, b"\xe9\x8e\x0d\x0a"]

    offset = 78 + 8 * len(sections) + 2
    table = b""
    for index, section in enumerate(sections):
        table += struct.pack(">LBBBB", offset, 0, 0, 0, index)
        offset += len(section)
    header = (
        b"teszt".ljust(32, b"\x00") + struct.pack(">HHLLLLLL", 0, 0, 0, 0, 0, 0, 0, 0)[:28]
        + b"BOOKMOBI" + struct.pack(">LL", (len(sections) * 2) - 1, 0) + struct.pack(">H", len(sections))
    )
    header = header[:60] + b"BOOKMOBI" + struct.pack(">LLH", 0, 0, len(sections))
    assert len(header) == 78
    path.write_bytes(header + table + b"\x00\x00" + b"".join(sections))
    return path


def html_book(text: str, charset: str = "utf-8") -> str:
    return (
        f'<html><head><meta http-equiv="content-type" content="text/html; charset={charset}"/></head><body>'
        f"<h1>Első fejezet</h1><p>{text}</p><mbp:pagebreak/><h1>Második fejezet</h1><p>Őszi szél, tűzön át.</p>"
        "</body></html>"
    )


@pytest.mark.parametrize("suffix", [".mobi", ".azw", ".azw3"])
def test_mobi_text_and_hungarian_accents_survive(tmp_path, suffix):
    path = make_mobi(tmp_path / f"konyv{suffix}", html_book(TEXT_HU))
    parsed = documents.parse_document(path)
    text = "\n".join(segment.text for segment in parsed.segments)
    assert "Árvíztűrő tükörfúrógép" in text
    assert "Öt szép szűz lány őrült írót nyúz" in text
    assert "Őszi szél, tűzön át." in text
    assert parsed.language == "hu"
    assert parsed.title == "Teszt könyv"
    assert len(parsed.segments) >= 1


def test_mobi_legacy_codepage_1252(tmp_path):
    # cp1252 has no ő/ű, so the old-codepage book only uses letters it can hold: á é í ó ö ú ü.
    text = "Régi könyv, tükörfúrógép és más szöveg a régi kódolásban, hogy elég hosszú legyen a nyelvfelismeréshez. " * 3
    html = f"<html><body><h1>Második fejezet</h1><p>{text}</p></body></html>"
    path = make_mobi(tmp_path / "regi.mobi", html, encoding="cp1252")
    combined = "\n".join(segment.text for segment in documents.parse_document(path).segments)
    assert "Régi könyv, tükörfúrógép és más szöveg" in combined
    assert "Második fejezet" in combined
    assert "\ufffd" not in combined
    assert "?" not in combined


def test_source_file_untouched_and_temp_dir_removed(tmp_path):
    path = make_mobi(tmp_path / "k.mobi", html_book(TEXT_HU))
    before = path.read_bytes()
    seen = []
    real = ebooks.convert_ebook

    def spy(p):
        converted = real(p)
        seen.append(converted.workdir)
        return converted

    documents.convert_ebook, original = spy, documents.convert_ebook
    try:
        documents.parse_document(path)
    finally:
        documents.convert_ebook = original
    assert path.read_bytes() == before
    assert seen and not seen[0].exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["k.mobi"]


@pytest.mark.parametrize("encryption", [1, 2])
def test_drm_protected_is_refused_with_reason(tmp_path, encryption):
    path = make_mobi(tmp_path / "vedett.azw", html_book(TEXT_HU), encryption=encryption)
    with pytest.raises(ebooks.DrmProtectedError, match="DRM-protected"):
        documents.parse_document(path)


def test_drm_error_is_a_value_error_naming_the_file(tmp_path):
    path = make_mobi(tmp_path / "vedett.mobi", html_book(TEXT_HU), encryption=2)
    with pytest.raises(ValueError, match="vedett.mobi"):
        documents.parse_document(path)


def test_not_a_mobi_and_truncated_files_fail_cleanly(tmp_path):
    junk = tmp_path / "junk.mobi"
    junk.write_bytes(b"this is not a book" * 20)
    with pytest.raises(ValueError, match="not a valid MOBI"):
        documents.parse_document(junk)
    empty = tmp_path / "empty.azw3"
    empty.write_bytes(b"")
    with pytest.raises(ValueError):
        documents.parse_document(empty)
    good = make_mobi(tmp_path / "ok.mobi", html_book(TEXT_HU)).read_bytes()
    cut = tmp_path / "cut.mobi"
    cut.write_bytes(good[:150])
    with pytest.raises(ValueError):
        documents.parse_document(cut)


def test_kfx_container_is_rejected_with_reason(tmp_path):
    path = tmp_path / "k.azw"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 200)
    with pytest.raises(ValueError, match="KFX"):
        documents.parse_document(path)


def test_kf8_style_epub_output_goes_through_epub_parser(tmp_path, monkeypatch):
    """KF8 books unpack to an EPUB; a real AZW3 is too large to hand-build, so the unpacker is faked here."""
    book = epub.EpubBook()
    book.set_identifier("x")
    book.set_title("Kéttannyelvű kötet")
    book.set_language("hu")
    book.add_author("Kovács Ödön")
    chapter = epub.EpubHtml(title="c", file_name="c1.xhtml", lang="hu")
    chapter.content = f"<html><body><p>{TEXT_HU}</p></body></html>"
    book.add_item(chapter)
    book.toc = (chapter,)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", chapter]
    built = tmp_path / "built.epub"
    epub.write_epub(str(built), book)
    src = make_mobi(tmp_path / "k.azw3", html_book("x"))

    monkeypatch.setattr(ebooks, "check_readable", lambda p: None)
    created = []

    def fake_unpack(infile, outdir, epubver="A"):
        created.append(Path(outdir))
        (Path(outdir) / "mobi8").mkdir()
        shutil.copyfile(built, Path(outdir) / "mobi8" / "book.epub")

    monkeypatch.setattr("mobi.kindleunpack.unpackBook", fake_unpack)
    parsed = documents.parse_document(src)
    assert parsed.title == "Kéttannyelvű kötet"
    assert parsed.author == "Kovács Ödön"
    assert "Árvíztűrő tükörfúrógép" in parsed.segments[0].text
    assert created and not created[0].exists()


def test_scan_picks_up_ebook_extensions_by_default():
    from teacher_common.config import get_settings

    assert {".mobi", ".azw", ".azw3"} <= set(get_settings().scan_extensions)


def test_failed_unpack_leaves_no_temp_dir(tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(ebooks.tempfile, "tempdir", str(tmp))

    def explode(infile, outdir, epubver="A"):
        raise RuntimeError("corrupt index")

    monkeypatch.setattr("mobi.kindleunpack.unpackBook", explode)
    src = make_mobi(tmp_path / "k.mobi", html_book(TEXT_HU))
    with pytest.raises(ValueError, match="corrupt index"):
        documents.parse_document(src)
    assert list(tmp.iterdir()) == []
