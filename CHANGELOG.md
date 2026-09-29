# Changelog

All notable changes to footprint-scanner. Newest first. Dates are when the change was made.

The tool only **discovers and documents**. No version sends removal requests, submits forms or
contacts site owners. Every request is filed manually after human review and customer approval.

---

## 2026-09-29: customer Word report, ID numbering, findings list

### Added
- **`footprint-scanner docx`:** builds the customer report as a Word document, to edit in Word and
  then print or *Save as PDF*.
  - Cover page with a summary by importance and site type.
  - **Findings list** after the cover page: ID, site, type, information shown, importance,
    proposed action. Each ID links to its item page, and the links survive Word's *Save as PDF*.
    The header repeats on every page.
  - One page per item: details, clickable link, **☐ Kaldır / ☐ Kalsın** (Remove / Keep) tick boxes,
    a comment line and the framed screenshot.
  - **Kaldırma Onayı** (Removal Authorization) page with name, signature and date lines.
  - Turkish (`--lang tr`) and English.
- **`--order id|severity`** for `docx` and `pdf`. The default `id` follows the tracker order;
  `severity` puts Critical first. Item numbers stay the same either way.
- `python-docx` dependency.

### Changed
- **Item numbers in customer reports are now the tracker's ID column.** "Madde 6" = ID 6 in Excel.
  Items were previously sorted by importance and renumbered 1, 2, 3…, which didn't match the
  spreadsheet. The small "Referans #…" line is gone because it's no longer needed.
- The authorization page of the Word report shows the item count with a link to the findings list
  instead of repeating the whole list, which would be about 25 pages for 1000+ items.
- README: examples now use Brave (the configured provider) and Chrome (the configured renderer);
  the new report workflow is documented.

## 2026-09-29: customer PDF report

### Added
- **`footprint-scanner pdf`:** fillable customer PDF with Remove/Keep radio buttons, a comment field
  per item, name/date fields and a signature line. It includes only `Confirmed` + `Likely` rows,
  never namesakes; `Unverified` rows are added only with `--include-unverified`. Internal Notes
  are never included.
- `report:` section in `config.yaml`: `prepared_by`, `lang`, `font`, `font_bold`.
- `reportlab` and `pillow` dependencies.

## 2026-09-29: fixes from the first real run

### Fixed
- **Crash on `user_agent` with Turkish characters** (e.g. `"Önder Uzel"`): HTTP headers must be
  ASCII. The value is now transliterated (`Onder Uzel`) with a warning, and checked at startup
  before any credits are spent.
- **Dropdowns silently lost in the output tracker.** After the tracker is saved in Excel, Excel
  stores list validations that point to the Lists sheet in an extended format that openpyxl drops.
  They're now read and written back, so the output copy keeps all 7 Findings dropdowns and value
  checking against Lists works again. The "Data Validation extension is not supported" warning is
  gone.

### Changed
- Tests use a clean fixture, `tests/fixtures/tracker_template.xlsx`, with fictional sample data
  only. They previously read `Footprint_Removal_Tracker.xlsx`, which is now a live client file.

## 2026-09-29: search provider and renderer choices

### Added
- **Renderer choice** (`fetch.renderer`): `playwright` (local browser), `zenrows` (ZenRows API) or
  `none`.
  - `fetch.browser_channel: chrome | msedge` uses an installed browser, so Playwright's Chromium
    download isn't needed.
  - ZenRows renderer: full-page screenshots and JS rendering via API, a per-run credit cap
    (`fetch.zenrows.max_credits`), retry on 429/503, and it stops cleanly on 401/402. robots.txt
    is still enforced; anti-bot and premium-proxy features aren't used.
- `ZENROWS_API_KEY` in `.env.example`.

### Changed
- `config.yaml`: search uses **Brave** (`providers: [brave]`); screenshots use the **installed
  Chrome** (`renderer: playwright`, `browser_channel: chrome`).

## 2026-09-29: initial version

### Added
- CLI commands `scan`, `rescan`, `queries`, `purge` (typer + rich).
- Authorization gate: refuses to scan unless Client Profile → *Signed authorization received?* is
  `Yes`, or `--i-have-authorization` is passed. The basis is logged.
- Identifiers from Client Profile (column B, `;`-separated) or `client.yaml`. Template
  placeholders are skipped.
- Query matrix: Turkish/ASCII name forms, name × city/employer/school, emails, usernames, 7 phone
  formats, grouped `site:` queries for social and professional sites and `brokers.yaml`.
  Deduplicated and prioritized; `--dry-run` shows cost.
- Search providers SerpAPI and Brave behind a pluggable interface: pagination, backoff, per-run
  API call budget, disk cache keyed by a hash of the query.
- Polite fetching: robots.txt, per-domain delay, timeouts, concurrency limit, full-page
  screenshots.
- Page analysis: Turkish-aware mention counting, where mentions appear, and exposed data types.
  TC Kimlik and IBAN are checksum-validated, with the 300-character proximity rule.
- Enrichment: RDAP owner/country, hosting country, Wayback archive check, removal/opt-out link
  finder.
- Suggested values validated against the Lists sheet: category, own account, content type,
  Match Confidence (`Unverified` / `Likely`, never `Confirmed`), severity, removal method, deindex
  fallback.
- Excel I/O by header name. Formulas, validations and conditional formatting are preserved, and
  the example row is replaced. Output is always a new timestamped copy.
- Re-scan: updates existing rows, detects *Re-appeared*, reports 404/410 as possibly removed, and
  only ever appends to Notes.
- Per-client folder `clients/<CASE-ID>/` holding the cache, evidence, run reports (`report.md`,
  `raw.jsonl`, `scan.log`) and tracker copies. `purge` deletes it.
