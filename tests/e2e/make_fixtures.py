"""Regenerate the Hungarian fixtures of the end-to-end trial (scripts/e2e.sh). The outputs are committed, so the trial
needs nothing from this script; run it only when the texts change:

    pip install PyMuPDF python-docx ebooklib pymupdf-fonts fonttools
    python tests/e2e/make_fixtures.py

Every text is invented, with facts no model can know, so a correct citation can only come from retrieval.
"""

from pathlib import Path

import fitz
import pymupdf_fonts  # noqa: F401 - registers the FiraGO font, which has ő and ű (the PDF base fonts do not)
from docx import Document
from ebooklib import epub

OUT = Path(__file__).parent / "fixtures"

MILL_PAGES = [
    "A vízimalmok működése\n\n"
    "A vízimalom a folyó vagy a patak sodrását használja fel a gabona őrlésére. A víz meghajtja a malomkereket, "
    "a kerék tengelye fogaskerekeken át forgatja a malomköveket, és a kövek között a búza liszté őrlődik. "
    "Az alulcsapott kerék lassú vízfolyáson is működik, a felülcsapott kerékhez viszont esés kell.",
    "A kőröshegyi malom története\n\n"
    "A kőröshegyi malmot 1847-ben építették. Kerekének pontosan tizennégy lapátja van, és a malmot "
    "Őri Ödön molnármester vezette negyvenegy éven át. A legendás kerék tölgyfából készült, tengelyét "
    "minden tavasszal disznózsírral kenték. Az árvíz 1879-ben elvitte a gátat, de a kerék épen maradt.",
]

TELESCOPE_PAGES = [
    "A Hold fázisai\n\n"
    "A Hold nem saját fényével világít, hanem a Nap fényét veri vissza. Ahogy a Föld körül kering, a megvilágított "
    "felének más és más részét látjuk: újhold, első negyed, telihold, majd utolsó negyed követi egymást. "
    "Egy teljes ciklus nagyjából huszonkilenc és fél napig tart.",
    "A gellérthegyi távcső\n\n"
    "A gellérthegyi távcső tükrének átmérője százhúsz centiméter. A műszert Ürögdi Győző csillagász tervezte, "
    "és a kupolát 1923 őszén adták át. A tükröt Bécsben öntötték, és szekérrel szállították a hegyre. "
    "A távcsővel az első évben kilenc üstököst figyeltek meg.",
]

FISH_CHAPTERS = [
    ("A ponty", "A pontyot sekély, iszapos tavakban tenyésztik. A ponty mindenevő hal: "
                "vízinövényeket, férgeket és rovarlárvákat eszik, és a meleg vizet kedveli."),
    ("A tiszai tokhal", "A tiszai tokhal tavasszal a Szamos torkolatánál ívik, a kavicsos mederben. "
                        "A halászok ezt a helyet Ikrás-zátonynak nevezik. Az ivadék ősszel úszik le a folyón."),
]

RECIPE = [
    "A bodzás őszibarack lekvár titkos receptje",
    "Egy kiló őszibarackhoz három fürt bodzavirág és fél kiló cukor kell. A titok a csipetnyi tárkony, "
    "amelyet csak a főzés végén szabad hozzáadni. A családi azonosító kódja BODZALEKVAR-7731.",
    "Forrón kell üvegbe tölteni, és dunsztban hagyni kihűlni, így két évig is eláll a kamrában.",
]


def _text_page(doc: fitz.Document, text: str, font: fitz.Font) -> fitz.Page:
    page = doc.new_page(width=595, height=842)
    writer = fitz.TextWriter(page.rect)
    writer.fill_textbox(fitz.Rect(60, 60, 535, 780), text, font=font, fontsize=14)
    writer.write_text(page)
    return page


def make_text_pdf(path: Path, pages: list[str], title: str) -> None:
    font = fitz.Font("figo")
    doc = fitz.open()
    for text in pages:
        _text_page(doc, text, font)
    doc.set_metadata({"title": title, "creationDate": "", "modDate": ""})
    doc.subset_fonts()
    doc.save(path, garbage=4, deflate=True, no_new_id=True)


def make_scanned_pdf(path: Path, pages: list[str], title: str, dpi: int = 150) -> None:
    """Every page is a picture of the text, grey like a scan: no text layer, so only OCR can read it."""
    font = fitz.Font("figo")
    source = fitz.open()
    doc = fitz.open()
    for text in pages:
        pix = _text_page(source, text, font).get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=pix.tobytes("png"))
    doc.set_metadata({"title": title, "creationDate": "", "modDate": ""})
    doc.save(path, garbage=4, deflate=True, no_new_id=True)


def make_epub(path: Path, chapters: list[tuple[str, str]], title: str) -> None:
    book = epub.EpubBook()
    book.set_identifier("e2e-halak-konyve")
    book.set_title(title)
    book.set_language("hu")
    items = []
    for number, (heading, body) in enumerate(chapters, start=1):
        item = epub.EpubHtml(title=heading, file_name=f"fejezet{number}.xhtml", lang="hu")
        item.content = f"<html><body><h1>{heading}</h1><p>{body}</p></body></html>"
        book.add_item(item)
        items.append(item)
    book.toc = items
    book.spine = items
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)


def make_docx(path: Path, paragraphs: list[str]) -> None:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(str(path))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    make_text_pdf(OUT / "vizimalmok.pdf", MILL_PAGES, "Vízimalmok")
    make_scanned_pdf(OUT / "szkennelt-csillagaszat.pdf", TELESCOPE_PAGES, "Csillagászat")
    make_epub(OUT / "halak-konyve.epub", FISH_CHAPTERS, "A halak könyve")
    make_docx(OUT / "titkos-receptek.docx", RECIPE)
    for path in sorted(OUT.iterdir()):
        print(f"{path.name}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
