import datetime as dt

from footprint_scanner.analyze import PageAnalysis
from footprint_scanner.excel_io import (
    H_CHECKED,
    H_CONFIDENCE,
    H_DATA_TYPES,
    H_EVIDENCE,
    H_MENTIONS,
    H_NOTES,
    H_RANK,
    H_SENT,
    H_STATUS,
    H_URL,
    ExistingRow,
    Tracker,
)
from footprint_scanner.fetch import FetchResult
from footprint_scanner.merge import Hit, Observation, merge_results, plan_rescan
from footprint_scanner.queries import Query
from footprint_scanner.search import SearchResult
from footprint_scanner.urls import url_key

TODAY = dt.date(2026, 10, 29)
GOOGLE = {"serpapi"}


def row(n, url, **values):
    return ExistingRow(row=n, url=url, key=url_key(url), values={H_URL: url, **values})


def hit(url, providers=("serpapi",), rank=None):
    h = Hit(url=url, key=url_key(url), providers=set(providers), found_by_google="serpapi" in providers)
    if rank:
        h.main_ranks = {providers[0]: rank}
    return h


def page(mentions, types=("Full name",)):
    return PageAnalysis(mentions=mentions, data_types=list(types), source="page")


def ok_fetch(url, shot=""):
    return FetchResult(url=url, status=200, html="<p>x</p>", screenshot=shot)


def updates_for(plan, r):
    return {u.header: u.value for u in plan.updates if u.row == r}


def test_merge_results_ranks_and_queries():
    main, other = Query('"Ahmet Yılmaz"', "name_main"), Query('"Ahmet Yilmaz"', "name")
    results = [
        (main, SearchResult("https://www.a.com/p/?utm_source=x", "t", "s", 7, "brave", main.text)),
        (main, SearchResult("https://a.com/p", "t", "s", 3, "serpapi", main.text)),
        (other, SearchResult("https://a.com/p#frag", "t", "s", 1, "serpapi", other.text)),
    ]
    hits = merge_results(results, GOOGLE)
    [h] = hits.values()
    assert h.queries == [main.text, other.text]
    assert h.rank(GOOGLE) == (3, "serpapi")  # Google preferred even though Brave also ranked it
    assert h.found_by_google
    only_brave = merge_results(results[:1], GOOGLE)
    assert next(iter(only_brave.values())).rank(GOOGLE) == (7, "brave")


def test_updates_existing_row_fields_only():
    r = row(3, "https://a.com/p", **{H_STATUS: "Requested", H_CONFIDENCE: "Confirmed", H_NOTES: "mine", H_RANK: 9})
    obs = {r.key: Observation(r.key, r.url, hit=hit(r.url, rank=2), fetch=ok_fetch(r.url, "evidence/0002_a.com_2026-10-29.png"),
                              analysis=page(4, ["Full name", "Phone"]))}
    plan = plan_rescan([r], obs, TODAY, main_query_ran=True, google_providers=GOOGLE)
    u = updates_for(plan, 3)
    assert u == {
        H_CHECKED: TODAY, H_MENTIONS: 4, H_DATA_TYPES: "Full name, Phone", H_RANK: 2,
        "Indexed in Google?": "Yes", H_EVIDENCE: "evidence/0002_a.com_2026-10-29.png",
    }
    assert H_STATUS not in u and H_CONFIDENCE not in u and H_NOTES not in u and H_SENT not in u
    assert plan.notes == [] and plan.reappeared == []


def test_evidence_not_overwritten():
    r = row(3, "https://a.com/p", **{H_EVIDENCE: "evidence/0002_a.com.png"})
    obs = {r.key: Observation(r.key, r.url, fetch=ok_fetch(r.url, "evidence/new.png"), analysis=page(1))}
    plan = plan_rescan([r], obs, TODAY, main_query_ran=True, google_providers=GOOGLE)
    assert H_EVIDENCE not in updates_for(plan, 3)


def test_rank_cleared_when_no_longer_found():
    r = row(3, "https://a.com/p", **{H_RANK: 5})
    obs = {r.key: Observation(r.key, r.url, fetch=ok_fetch(r.url), analysis=page(1))}
    plan = plan_rescan([r], obs, TODAY, main_query_ran=True, google_providers=GOOGLE)
    assert updates_for(plan, 3)[H_RANK] is None
    # ...but not when the main query didn't run (e.g. budget exhausted).
    plan = plan_rescan([r], obs, TODAY, main_query_ran=False, google_providers=GOOGLE)
    assert H_RANK not in updates_for(plan, 3)


def test_brave_rank_noted_once():
    r = row(3, "https://a.com/p")
    obs = {r.key: Observation(r.key, r.url, hit=hit(r.url, ("brave",), rank=4))}
    plan = plan_rescan([r], obs, TODAY, main_query_ran=True, google_providers=GOOGLE, provider_labels={"brave": "Brave"})
    assert plan.notes == [(3, "[scanner 2026-10-29] Rank source: Brave (not Google).")]
    r.values[H_NOTES] = "x\n[scanner 2026-09-29] Rank source: Brave (not Google)."
    plan = plan_rescan([r], obs, TODAY, main_query_ran=True, google_providers=GOOGLE, provider_labels={"brave": "Brave"})
    assert plan.notes == []


def test_deindexed_reappears_only_when_found_in_search():
    r = row(3, "https://a.com/p", **{H_STATUS: "Deindexed only"})
    live_only = {r.key: Observation(r.key, r.url, fetch=ok_fetch(r.url), analysis=page(3))}
    plan = plan_rescan([r], live_only, TODAY, main_query_ran=True, google_providers=GOOGLE)
    assert plan.reappeared == [] and H_STATUS not in updates_for(plan, 3)

    in_search = {r.key: Observation(r.key, r.url, hit=hit(r.url), fetch=ok_fetch(r.url), analysis=page(3))}
    plan = plan_rescan([r], in_search, TODAY, main_query_ran=True, google_providers=GOOGLE)
    assert updates_for(plan, 3)[H_STATUS] == "Re-appeared"
    assert plan.reappeared[0]["was"] == "Deindexed only"
    assert plan.notes[0][1].startswith("[scanner 2026-10-29] Re-appeared (was Deindexed only)")


def test_removed_reappears_when_live_with_mentions():
    r = row(3, "https://a.com/p", **{H_STATUS: "Removed"})
    obs = {r.key: Observation(r.key, r.url, fetch=ok_fetch(r.url), analysis=page(2))}
    plan = plan_rescan([r], obs, TODAY, main_query_ran=True, google_providers=GOOGLE)
    assert updates_for(plan, 3)[H_STATUS] == "Re-appeared"
    assert "HTTP 200, 2 mention(s)" in plan.reappeared[0]["reason"]

    # Page up but the name is gone: still removed.
    obs = {r.key: Observation(r.key, r.url, fetch=ok_fetch(r.url), analysis=page(0, []))}
    plan = plan_rescan([r], obs, TODAY, main_query_ran=True, google_providers=GOOGLE)
    assert plan.reappeared == []


def test_404_reported_as_possibly_removed_without_status_change():
    r = row(4, "https://a.com/gone", **{H_STATUS: "Requested"})
    obs = {r.key: Observation(r.key, r.url, fetch=FetchResult(url=r.url, status=404))}
    plan = plan_rescan([r], obs, TODAY, main_query_ran=True, google_providers=GOOGLE)
    assert plan.possibly_removed == [
        {"row": 4, "id": 3, "url": r.url, "http_status": 404, "status": "Requested"}
    ]
    assert H_STATUS not in updates_for(plan, 4)
    assert updates_for(plan, 4) == {H_CHECKED: TODAY}


def test_plan_applies_cleanly_to_template(template_copy, tmp_path):
    from footprint_scanner.pipeline import apply_rescan_plan

    t = Tracker(template_copy)
    t.clear_example_row()
    t.set(2, H_URL, "https://a.com/p")
    t.set(2, H_STATUS, "Removed")
    t.set(2, H_CONFIDENCE, "Confirmed")
    t.set(2, H_SENT, dt.date(2026, 9, 1))
    t.set(2, H_NOTES, "Client confirmed via phone")
    [r] = t.existing_rows()
    obs = {r.key: Observation(r.key, r.url, hit=hit(r.url, rank=1), fetch=ok_fetch(r.url), analysis=page(2))}
    plan = plan_rescan([r], obs, TODAY, main_query_ran=True, google_providers=GOOGLE)
    apply_rescan_plan(t, plan)
    out = t.save(tmp_path / "rescanned.xlsx")
    t2 = Tracker(out)
    assert t2.get(2, H_STATUS) == "Re-appeared"
    assert t2.get(2, H_CONFIDENCE) == "Confirmed"
    assert t2.get(2, H_SENT) == dt.datetime(2026, 9, 1)
    assert t2.get(2, H_NOTES).startswith("Client confirmed via phone\n[scanner 2026-10-29] Re-appeared")
    assert t2.get(2, H_RANK) == 1
    assert t2.get(2, "ID") == '=IF(C2="","",ROW()-1)'
