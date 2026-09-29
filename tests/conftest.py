import shutil
from pathlib import Path

import pytest

from footprint_scanner.identifiers import ClientIdentifiers
from footprint_scanner.sites import load_sites

ROOT = Path(__file__).resolve().parents[1]
# Clean copy of the tracker template with sample data only. Never point tests at a live tracker.
TEMPLATE = ROOT / "tests" / "fixtures" / "tracker_template.xlsx"


@pytest.fixture
def template_copy(tmp_path: Path) -> Path:
    if not TEMPLATE.exists():
        pytest.skip("Footprint_Removal_Tracker.xlsx template not present")
    dst = tmp_path / "Footprint_Removal_Tracker.xlsx"
    shutil.copy(TEMPLATE, dst)
    return dst


@pytest.fixture
def sites():
    return load_sites(ROOT / "brokers.yaml", ROOT / "domain_categories.yaml")


@pytest.fixture
def ident() -> ClientIdentifiers:
    return ClientIdentifiers(
        case_id="CASE-TEST-001",
        full_name="Ahmet Yılmaz",
        name_variants=["Ahmet Kemal Yılmaz", "A. Yılmaz"],
        cities=["İstanbul"],
        phones=["+90 532 123 45 67"],
        emails=["ahmet.yilmaz@example.com"],
        usernames=["@ahmetyilmaz34"],
        employers=["Acme Yazılım"],
        schools=["Boğaziçi Üniversitesi"],
        relatives=["Ayşe Yılmaz"],
        addresses=["Bağdat Caddesi No: 12 Kadıköy"],
        authorized=True,
    )
