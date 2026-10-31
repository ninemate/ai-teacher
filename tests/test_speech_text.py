import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "voice" / "app"))

from speech_text import prepare_for_speech  # noqa: E402


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("## A sejt felépítése\n\nA **sejtmag** a sejt _irányítója_.", "A sejt felépítése. A sejtmag a sejt _irányítója_."),
        ("Lépések:\n1. fény elnyelése\n2. víz bontása", "Lépések: 1. fény elnyelése. 2. víz bontása."),
        ("- első pont\n* második pont\n• harmadik", "első pont. második pont. harmadik."),
        ("Lásd [a tankönyvet](https://example.org/konyv) és https://example.org/x oldalt.", "Lásd a tankönyvet és oldalt."),
        ("A magyar ragozó nyelv [1]. A sorrend kötött [1, 3]. Gyakorolj naponta [forrás nélkül].",
         "A magyar ragozó nyelv. A sorrend kötött. Gyakorolj naponta."),
        ("Kb. 5 db alma, kb. 3 kg.", "Körülbelül 5 darab alma, körülbelül 3 kg."),
        ("Caesar i. e. 44-ben halt meg, Szent István i. sz. 1000-ben.",
         "Caesar időszámításunk előtt 44-ben halt meg, Szent István időszámításunk szerint 1000-ben."),
        ("> Idézet a könyvből", "Idézet a könyvből."),
        ("| Fogalom | Jelentés |\n|---|---|\n| sejt | egység |", "Fogalom, Jelentés. sejt, egység."),
        ("```\nkód\n```\nVége!", "kód. Vége!"),
        ("pl. a fotoszintézis stb.", "pl. a fotoszintézis stb."),  # espeak-ng reads these itself
        ("**  **\n\n#", ""),
    ],
)
def test_prepare_for_speech(text, spoken):
    assert prepare_for_speech(text) == spoken


def test_words_that_merely_contain_an_abbreviation_are_kept():
    assert prepare_for_speech("A dbx és a kbit maradjon.") == "A dbx és a kbit maradjon."
