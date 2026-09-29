"""runs/<timestamp>/report.md"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .pipeline import PROVIDER_LABELS, RunResult


def _md(s: object) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def render_report(r: RunResult) -> str:
    L: list[str] = []
    add = L.append
    add(f"# Footprint {r.mode} report: {r.case_id}")
    add("")
    add(f"- Run started: {r.started:%Y-%m-%d %H:%M}")
    add(f"- Mode: `{r.mode}`")
    add(f"- Authorization basis: {r.authorization_basis}")
    add(f"- Providers: {', '.join(PROVIDER_LABELS.get(p, p) for p in r.providers)}")
    add(f"- Input tracker: `{r.input_tracker.name}` (not modified)")
    add(f"- Output tracker: `{r.output_tracker.name if r.output_tracker else '(not saved)'}`")
    if r.example_row_replaced:
        add("- The template's EXAMPLE ROW was replaced.")
    add("")
    add("> This tool only discovers and documents. **No removal request was sent.** "
        "Every suggested value (Match Confidence, Severity, Removal Method, ...) must be reviewed by a person, "
        "and requests are filed manually after that review.")
    add("")

    add("## Summary")
    add("")
    add("| Item | Count |")
    add("|---|---|")
    ran = sum(1 for q in r.query_logs if not q.skipped)
    add(f"| Queries in matrix | {len(r.queries)} |")
    add(f"| Query × provider searches run | {ran} |")
    add(f"| Skipped (budget) | {sum(1 for q in r.query_logs if q.skipped)} |")
    add(f"| API calls (credits) used | {sum(r.api_calls.values())} of budget {r.budget_max} |")
    for p, n in sorted(r.api_calls.items()):
        add(f"| &nbsp;&nbsp;{PROVIDER_LABELS.get(p, p)} | {n} |")
    add(f"| Cached responses reused (free) | {r.cache_hits} |")
    if r.renderer == "zenrows":
        add(f"| ZenRows credits used (screenshots / JS rendering) | {r.render_credits} |")
    add(f"| Unique URLs found | {r.unique_urls} |")
    add(f"| New rows added | {len(r.new_rows)} |")
    add(f"| Found again, already in tracker | {len(r.already_tracked)} |")
    if r.mode == "rescan":
        add(f"| Existing rows re-checked | {r.rescanned} |")
        add(f"| Re-appeared | {len(r.reappeared)} |")
        add(f"| Possibly removed (404/410) | {len(r.possibly_removed)} |")
    add(f"| Errors | {len(r.errors)} |")
    add("")
    if r.budget_exhausted:
        add("**The API call budget ran out; some queries were skipped.** Raise `search.max_api_calls` "
            "or re-run (cached responses cost nothing).")
        add("")

    for w in r.warnings:
        add(f"> Warning: {w}")
        add("")

    if r.mode == "rescan":
        add("## Re-appeared")
        add("")
        if r.reappeared:
            add("| ID | URL | Was | Why |")
            add("|---|---|---|---|")
            for x in r.reappeared:
                add(f"| {x['id']} | {_md(x['url'])} | {_md(x['was'])} | {_md(x['reason'])} |")
        else:
            add("None.")
        add("")
        add("## Possibly removed (HTTP 404/410, Status not changed)")
        add("")
        if r.possibly_removed:
            add("| ID | URL | HTTP | Current Status |")
            add("|---|---|---|---|")
            for x in r.possibly_removed:
                add(f"| {x['id']} | {_md(x['url'])} | {x['http_status']} | {_md(x['status'])} |")
        else:
            add("None.")
        add("")

    add("## New rows")
    add("")
    if r.new_rows:
        sev = Counter(x["severity"] for x in r.new_rows)
        add("By suggested severity: " + ", ".join(f"{k} {sev[k]}" for k in ("Critical", "High", "Medium", "Low") if sev[k]))
        add("")
        add("| ID | URL | Category | Confidence | Severity | Data types |")
        add("|---|---|---|---|---|---|")
        for x in r.new_rows:
            add(f"| {x['id']} | {_md(x['url'])} | {_md(x['category'])} | {_md(x['confidence'])} | "
                f"{_md(x['severity'])} | {_md(x['data_types'])} |")
    else:
        add("None.")
    add("")

    if r.already_tracked:
        add("## Found again (already in tracker)")
        add("")
        for x in r.already_tracked:
            add(f"- ID {x['id']}: {x['url']}")
        add("")

    add("## Queries run")
    add("")
    add("| # | Kind | Query | Provider | Results | Note |")
    add("|---|---|---|---|---|---|")
    for i, q in enumerate(r.query_logs, 1):
        note = "skipped (budget)" if q.skipped else (q.error or "")
        add(f"| {i} | {q.kind} | {_md(q.query)} | {q.provider} | {q.results} | {_md(note)} |")
    add("")

    add("## Errors")
    add("")
    if r.errors:
        for e in r.errors:
            add(f"- {_md(e)}")
    else:
        add("None.")
    add("")
    add("Per-URL details (everything extracted) are in `raw.jsonl` next to this report.")
    return "\n".join(L) + "\n"


def write_report(r: RunResult) -> Path:
    path = r.run_dir / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(r), encoding="utf-8")
    return path
