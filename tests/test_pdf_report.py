import datetime as dt
import re
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from footprint_scanner.excel_io import Tracker
from footprint_scanner.pdf_report import FONT_CANDIDATES, generate, select_items, tr, tr_list

pytestmark = pytest.mark.skipif(
    not any(Path(r).exists() for r, _ in FONT_CANDIDATES), reason="no TTF font with Turkish glyphs on this system"
)

ROWS = [
    # url, confidence, severity, category, data types, evidence
    ("https://www.spokeo.com/Ahmet-Yilmaz", "Likely", "High", "Data broker / people search", "Full name, Phone, Relatives", True),
    ("https://www.hurriyet.com.tr/gundem/ahmet-yilmaz", "Confirmed", "Critical", "News / media", "Full name, TC Kimlik No", True),
    ("https://www.instagram.com/ahmetyilmaz34", "Confirmed", "Medium", "Social media", "Full name, Username", False),
    ("https://other.example/ahmet-yilmaz", "Unverified", "Low", "Other", "Full name", False),
    ("https://namesake.example/ahmet-yilmaz", "Namesake (not client)", "High", "Other", "Full name, Phone", False),
]


def make_shot(path: Path, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (1366, height), "white")
    d = ImageDraw.Draw(im)
    for y in range(0, height, 120):
        d.rectangle([40, y + 20, 1300, y + 90], outline="black")
    im.save(path)


@pytest.fixture
def filled_tracker(template_copy):
    t = Tracker(template_copy)
    t.clear_example_row()
    base = template_copy.parent
    for (url, conf, sev, cat, types, shot), row in zip(ROWS, t.allocate_rows(len(ROWS)), strict=True):
        t.set(row, "Site Link", url)
        t.set(row, "Domain", re.sub(r"^https://(www\.)?([^/]+).*$", r"\2", url))
        t.set(row, "Match Confidence", conf)
        t.set(row, "Severity", sev)
        t.set(row, "Site Category", cat)
        t.set(row, "Data Types Exposed", types)
        t.set(row, "Mentioned Places (on page)", "Title, Body")
        t.set(row, "Removal Method", "Site opt-out form" if "broker" in cat else "KVKK request (Law 6698)")
        t.set(row, "Status", "Not started")
        t.set(row, "Date Found", dt.date(2026, 9, 29))
        t.set(row, "Notes", "INTERNAL scanner note with query text")
        if shot:
            rel = f"evidence/{row - 1:04d}_shot.png"
            make_shot(base / rel, 6000 if "spokeo" in url else 700)
            t.set(row, "Evidence (screenshot path)", rel)
    out = t.save(base / "reviewed.xlsx")
    return out


def pdf_text_markers(path: Path) -> bytes:
    return path.read_bytes()


def test_selection_excludes_namesakes_and_unverified(filled_tracker):
    items = select_items(Tracker(filled_tracker))
    assert [i.id for i in items] == [1, 2, 3]  # tracker order; numbers are tracker IDs
    assert [i.get("Match Confidence") for i in items] == ["Likely", "Confirmed", "Confirmed"]
    assert items[0].evidence is not None and items[2].evidence is None
    by_severity = select_items(Tracker(filled_tracker), order="severity")
    assert [(i.id, i.get("Severity")) for i in by_severity] == [(2, "Critical"), (1, "High"), (3, "Medium")]
    with_unverified = select_items(Tracker(filled_tracker), include_unverified=True)
    assert len(with_unverified) == 4
    assert all(i.get("Match Confidence") != "Namesake (not client)" for i in with_unverified)


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_generates_fillable_pdf(filled_tracker, ident, tmp_path, lang):
    out = tmp_path / f"report_{lang}.pdf"
    path, n = generate(filled_tracker, ident, out, lang=lang, prepared_by="Uzel Privacy", today=dt.date(2026, 10, 1))
    assert n == 3
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    # Remove/Keep radio groups and comment field per item, plus signature-page fields.
    for item_id in (1, 2, 3):
        assert f"decision_{item_id}".encode() in data
        assert f"comment_{item_id}".encode() in data
    assert b"decision_4" not in data and b"decision_5" not in data
    assert b"auth_name" in data and b"auth_sign_date" in data
    # Internal notes never go to the customer.
    assert b"INTERNAL scanner note" not in data
    # cover + 3 item pages + authorization page (+ possible overflow)
    assert len(re.findall(rb"/Type /Page\b", data)) >= 5


def test_translations():
    assert tr("Critical", "tr") == "Kritik"
    assert tr("KVKK request (Law 6698)", "tr") == "KVKK başvurusu (6698 sayılı Kanun)"
    assert tr_list("Full name, Phone, TC Kimlik No", "tr") == "Ad soyad, Telefon, TC kimlik no"
    assert tr("Critical", "en") == "Critical"


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_generates_docx(filled_tracker, ident, tmp_path, lang):
    import zipfile

    from footprint_scanner.docx_report import generate as generate_docx

    out = tmp_path / f"report_{lang}.docx"
    path, n = generate_docx(filled_tracker, ident, out, lang=lang, prepared_by="Vias Yazılım",
                            today=dt.date(2026, 10, 1))
    assert n == 3
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
        media = [f for f in z.namelist() if f.startswith("word/media/")]
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
    assert len(media) == 2  # two rows have screenshots
    assert "Vias Yazılım" in xml
    assert xml.count("\u2610") == 6  # Remove + Keep box on each of 3 items
    assert "INTERNAL scanner note" not in xml
    assert "namesake.example" not in xml and "other.example" not in xml
    assert "https://www.hurriyet.com.tr/gundem/ahmet-yilmaz" in rels  # clickable link
    assert ("Kaldırma Onayı" if lang == "tr" else "Removal Authorization") in xml
    assert xml.count('w:pageBreakBefore') == 5  # overview + 3 items + authorization page
    # Overview: each number links to a bookmark on its item page; headings use the tracker ID.
    for item_id in (1, 2, 3):
        assert f'w:anchor="item_{item_id}"' in xml and f'w:name="item_{item_id}"' in xml
    assert 'w:name="findings_list"' in xml and 'w:anchor="findings_list"' in xml
    assert "item_4" not in xml and "item_5" not in xml
    item_word = "Madde" if lang == "tr" else "Item"
    assert f"{item_word} 2 · hurriyet.com.tr" in xml
