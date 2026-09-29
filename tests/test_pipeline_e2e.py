"""End-to-end scan + rescan against the real template, with every network call mocked."""

import datetime as dt
import json

import httpx
import openpyxl
import pytest
import respx
from typer.testing import CliRunner

from footprint_scanner.cli import app
from footprint_scanner.config import Config
from footprint_scanner.enrich import Enricher
from footprint_scanner.excel_io import Tracker
from footprint_scanner.pipeline import AuthorizationError, Scanner, check_authorization
from footprint_scanner.report import write_report
from footprint_scanner.search import make_providers

BROKER_URL = "https://www.spokeo.com/Ahmet-Yilmaz/Istanbul?utm_source=g"
NEWS_URL = "https://www.hurriyet.com.tr/gundem/ahmet-yilmaz-haberi-123"
IG_URL = "https://www.instagram.com/ahmetyilmaz34/"
GONE_URL = "https://old.example.org/kisi/ahmet"

BROKER_HTML = """<html><head><title>Ahmet Yılmaz, İstanbul - Spokeo</title></head><body>
<h1>Ahmet Yılmaz</h1><p>Phone: 0532 123 45 67. Relatives: Ayşe Yılmaz.</p>
<a href="/privacy">Privacy</a><footer>© 2024 Spokeo, Inc.</footer></body></html>""" + "<p>" + "x " * 300 + "</p>"
NEWS_HTML = """<html><head><title>Haber</title></head><body><h2>Başlık</h2>
<p>Dün Ahmet Yilmaz açıklama yaptı.</p>""" + "<p>" + "metin " * 200 + "</p></body></html>"


def serp_response(request):
    q = request.url.params["q"]
    if q == '"Ahmet Yılmaz"':
        organic = [
            {"link": NEWS_URL, "title": "Haber", "snippet": "Ahmet Yılmaz"},
            {"link": BROKER_URL, "title": "Ahmet Yılmaz - Spokeo", "snippet": "Age 40, İstanbul"},
        ]
    elif "instagram" in q:
        organic = [{"link": IG_URL, "title": "Ahmet (@ahmetyilmaz34)", "snippet": "İstanbul"}]
    else:
        organic = []
    return httpx.Response(200, json={"organic_results": organic})


def mock_web(pages):
    respx.get("https://serpapi.com/search.json").mock(side_effect=serp_response)
    # Specific route first: respx uses the first matching route.
    respx.get("https://www.instagram.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    respx.get(url__regex=r"https://[^/]+/robots\.txt").mock(return_value=httpx.Response(404))
    for url, resp in pages.items():
        respx.get(url).mock(return_value=resp)
    respx.get(url__regex=r"https://rdap\.org/domain/.*").mock(return_value=httpx.Response(404))
    respx.get(url__regex=r"https://rdap\.org/ip/.*").mock(return_value=httpx.Response(200, json={"country": "US"}))
    respx.get("https://archive.org/wayback/available").mock(
        return_value=httpx.Response(200, json={"archived_snapshots": {"closest": {"available": True}}})
    )


def html(body):
    return httpx.Response(200, html=body, headers={"content-type": "text/html; charset=utf-8"})


@pytest.fixture
def cfg(tmp_path):
    c = Config(base_dir=tmp_path)
    c.search.request_delay = 0
    c.search.max_results_per_query = 10
    c.search.site_query_mode = "grouped"
    c.fetch.per_domain_delay = 0
    c.fetch.screenshots = False
    c.api_keys = {"serpapi": "TESTKEY-SERPAPI"}
    return c


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    async def fake_resolve(self, host):
        return "93.184.216.34"

    monkeypatch.setattr(Enricher, "_resolve", fake_resolve)


def scanner(cfg, ident, sites, tracker, mode, now):
    providers = make_providers(["serpapi"], cfg.api_keys, {"serpapi": cfg.search.serpapi})
    return Scanner(cfg, ident, sites, providers, mode=mode, tracker_path=tracker, authorization_basis="test", now=now)


def test_authorization_gate(ident):
    ident.authorized = False
    with pytest.raises(AuthorizationError):
        check_authorization(ident, override=False)
    assert "--i-have-authorization" in check_authorization(ident, override=True)
    ident.authorized = True
    assert "Yes" in check_authorization(ident, override=False)


@respx.mock
async def test_scan_then_rescan(cfg, ident, sites, template_copy):
    ident.case_id = "CASE-2026-001"
    mock_web({BROKER_URL.split("?")[0]: html(BROKER_HTML), NEWS_URL: html(NEWS_HTML)})

    # ---------------- scan
    now = dt.datetime(2026, 9, 29, 14, 30)
    result = await scanner(cfg, ident, sites, template_copy, "scan", now).run()
    out = result.output_tracker
    assert out.name == "Footprint_Removal_Tracker_2026-09-29_1430.xlsx"
    assert out.parent == cfg.client_dir("CASE-2026-001")
    assert result.example_row_replaced
    assert len(result.new_rows) == 3

    t = Tracker(out)
    rows = {r.url: r for r in t.existing_rows()}
    news = rows[NEWS_URL].values
    broker = rows["https://www.spokeo.com/Ahmet-Yilmaz/Istanbul"].values
    ig = rows["https://www.instagram.com/ahmetyilmaz34"].values

    assert rows[NEWS_URL].row == 2  # rank 1 goes first, into the old example row
    assert news["Google Rank for Name"] == 1 and news["Indexed in Google?"] == "Yes"
    assert news["Site Category"] == "News / media"
    assert news["Removal Method"] == "KVKK request (Law 6698)"
    assert news["Match Confidence"] == "Unverified" and news["Severity"] == "Low"
    assert news["Mentions on Page"] == 1 and news["Mentioned Places (on page)"] == "Body, URL slug"
    assert news["Archived Copies?"] == "Yes" and news["Jurisdiction / Hosting Country"] == "USA (hosting)"
    assert news["Status"] == "Not started" and news["Date Found"] == dt.datetime(2026, 9, 29)

    assert broker["Site Category"] == "Data broker / people search"
    assert broker["Match Confidence"] == "Likely"  # name + known phone / relative / city
    assert broker["Severity"] == "High"
    assert broker["Removal Method"] == "Site opt-out form"
    assert broker["Removal Contact / Form URL"] == "https://www.spokeo.com/optout"
    assert broker["Deindex Fallback"] == "Google 'Results about you' / personal info form"
    assert broker["Site Owner"] == "Spokeo, Inc"
    assert "Phone" in broker["Data Types Exposed"] and "Relatives" in broker["Data Types Exposed"]
    assert broker["Mentions on Page"] == 2  # title + h1; "Ayşe Yılmaz" is a relative, not a mention

    assert ig["Client's Own Account?"] == "Yes"
    assert ig["Removal Method"] == "Client deletes own account"
    assert ig["Mentions on Page"] is None  # robots.txt blocked: snippet only
    assert "\nNot fetched: robots.txt disallows" in ig["Notes"]
    assert ig["Google Rank for Name"] is None

    for r in rows.values():
        assert r.values["Match Confidence"] != "Confirmed"
        assert r.values["ID"].startswith("=IF(")

    run_dir = result.run_dir
    raw = [json.loads(line) for line in (run_dir / "raw.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(raw) == 3 and all("queries" in r for r in raw)
    report = write_report(result).read_text(encoding="utf-8")
    assert "No removal request was sent" in report and "API calls (credits) used | " in report
    for f in cfg.client_dir("CASE-2026-001").rglob("*"):
        if f.is_file() and f.suffix in (".json", ".jsonl", ".md", ".log"):
            assert "TESTKEY-SERPAPI" not in f.read_text(encoding="utf-8")

    # Reviewer edits the copy: news is removed, broker deindexed, plus a manually added gone URL.
    wb = openpyxl.load_workbook(out)
    ws = wb["Findings"]
    ws.cell(rows[NEWS_URL].row, 25).value = "Removed"
    ws.cell(rows[NEWS_URL].row, 8).value = "Confirmed"
    ws.cell(rows[NEWS_URL].row, 28).value = "Reviewer note"
    ws.cell(rows["https://www.spokeo.com/Ahmet-Yilmaz/Istanbul"].row, 25).value = "Deindexed only"
    ws.cell(5, 3).value = GONE_URL
    ws.cell(5, 25).value = "Requested"
    reviewed = out.with_name("reviewed.xlsx")
    wb.save(reviewed)

    # ---------------- rescan: news gone from search but page live; broker back in search.
    respx.get(GONE_URL).mock(return_value=httpx.Response(404))
    later = dt.datetime(2026, 10, 29, 9, 0)
    r2 = await scanner(cfg, ident, sites, reviewed, "rescan", later).run()
    assert {x["url"] for x in r2.reappeared} == {NEWS_URL, "https://www.spokeo.com/Ahmet-Yilmaz/Istanbul"}
    assert [x["url"] for x in r2.possibly_removed] == [GONE_URL]
    assert r2.new_rows == [] and r2.rescanned == 4

    t2 = Tracker(r2.output_tracker)
    rows2 = {r.url: r.values for r in t2.existing_rows()}
    n2 = rows2[NEWS_URL]
    assert n2["Status"] == "Re-appeared" and n2["Match Confidence"] == "Confirmed"
    assert n2["Notes"].startswith("Reviewer note\n[scanner 2026-10-29] Re-appeared (was Removed)")
    assert n2["Last Checked"] == dt.datetime(2026, 10, 29)
    assert rows2[GONE_URL]["Status"] == "Requested"
    assert rows2[GONE_URL]["Last Checked"] == dt.datetime(2026, 10, 29)
    assert r2.output_tracker.name == "reviewed_2026-10-29_0900.xlsx"


@respx.mock
def test_cli_refuses_without_authorization(template_copy, tmp_path):
    res = CliRunner().invoke(app, ["scan", "--tracker", str(template_copy), "--config", str(tmp_path / "none.yaml")])
    assert res.exit_code == 3
    assert "Refusing to scan" in res.output
    assert not respx.calls  # nothing sent anywhere


def test_cli_purge(tmp_path):
    (tmp_path / "config.yaml").write_text("paths:\n  clients_dir: clients\n", encoding="utf-8")
    folder = tmp_path / "clients" / "CASE-9"
    (folder / "evidence").mkdir(parents=True)
    (folder / "evidence" / "0001_a.png").write_bytes(b"x")
    (folder / "cache" / "search" / "serpapi").mkdir(parents=True)
    (folder / "cache" / "search" / "serpapi" / "k.json").write_text("{}")
    (tmp_path / "clients" / "CASE-10").mkdir()
    res = CliRunner().invoke(app, ["purge", "--client", "CASE-9", "--config", str(tmp_path / "config.yaml")], input="n\n")
    assert res.exit_code == 1 and folder.exists()
    res = CliRunner().invoke(app, ["purge", "--client", "CASE-9", "--config", str(tmp_path / "config.yaml"), "--yes"])
    assert res.exit_code == 0 and not folder.exists()
    assert (tmp_path / "clients" / "CASE-10").exists()
    res = CliRunner().invoke(app, ["purge", "--client", "../..", "--config", str(tmp_path / "config.yaml"), "--yes"])
    assert res.exit_code == 2
