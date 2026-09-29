import openpyxl
import pytest

from footprint_scanner.identifiers import load_from_tracker, load_from_yaml


def test_reads_template_profile(template_copy):
    ident = load_from_tracker(template_copy)
    assert ident.case_id == "CASE-2026-001"
    assert ident.full_name == "Ahmet Yılmaz"
    assert ident.cities == ["İstanbul", "Ankara"]
    assert ident.phones == []  # '+90 5xx xxx xx xx' placeholder skipped
    assert ident.authorized is False
    # 'Ahmet Yilmaz' folds to the full name, so it's not repeated.
    assert ident.names == ["Ahmet Yılmaz", "A. Yılmaz", "Ahmet Kemal Yılmaz"]


def test_authorization_yes(template_copy):
    wb = openpyxl.load_workbook(template_copy)
    wb["Client Profile"]["B19"] = "Yes"
    wb["Client Profile"]["B12"] = "a@b.com; not-an-email"
    wb.save(template_copy)
    ident = load_from_tracker(template_copy)
    assert ident.authorized is True
    assert ident.emails == ["a@b.com"]


def test_yaml_input(tmp_path):
    p = tmp_path / "client.yaml"
    p.write_text(
        "case_id: CASE-9\nfull_name: Ayşe Kaya\nname_variants: [Ayse Kaya]\n"
        "phones: '0532 111 22 33; +90 5xx xxx xx xx'\nauthorized: yes\ndob: 1985-03-15\n",
        encoding="utf-8",
    )
    ident = load_from_yaml(p)
    assert ident.full_name == "Ayşe Kaya"
    assert ident.phones == ["0532 111 22 33"]
    assert ident.authorized is True
    assert str(ident.dob) == "1985-03-15"


def test_missing_name_raises(tmp_path):
    p = tmp_path / "client.yaml"
    p.write_text("case_id: X\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_from_yaml(p)
