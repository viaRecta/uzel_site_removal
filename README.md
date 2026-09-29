# footprint-scanner

Finds where a client appears online and records each finding in the
**Footprint Removal Tracker** (`Footprint_Removal_Tracker.xlsx`). Run it only for clients who have
given signed consent.

> **Discovery and documentation only.** The tool never contacts a site, never submits an opt-out
> form and never sends a removal request. Every value it fills in (Match Confidence, Severity,
> Removal Method, and so on) is a *suggestion*. A person reviews each row, and every removal
> request is filed **manually** after that review. The tool never sets Match Confidence to
> `Confirmed`. Only you do that.

---

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

### Screenshots and JS rendering

Screenshots and JS-heavy pages need a renderer, set by `fetch.renderer` in `config.yaml`:

| `fetch.renderer` | What it does | Needs |
|---|---|---|
| `zenrows` (configured) | [ZenRows](https://www.zenrows.com) API takes the full-page screenshot and renders JS-heavy pages. | `ZENROWS_API_KEY` in `.env`. 5 credits per screenshot or render; capped per run by `fetch.zenrows.max_credits`. |
| `playwright` + `browser_channel: msedge` | Local headless Microsoft Edge (installed on every Windows 10/11). Nothing to download, free. | Nothing. |
| `playwright` + `browser_channel: ""` | Playwright's own Chromium. | `playwright install chromium` (large download; it can time out behind some networks). |
| `none` | No screenshots; static HTML only. | Nothing. |

With every renderer, plain page HTML is fetched **directly** from the site (free), robots.txt is
always respected, and only robots-allowed pages are sent for a screenshot. ZenRows' anti-bot and
premium-proxy features are **not** used. If the renderer fails (for example no browser, or no
ZenRows credits), the run continues without screenshots. Each affected row says why, and the report
shows a warning.

### API keys

Copy `.env.example` to `.env` and fill in the providers you use:

```
SERPAPI_API_KEY=...     # https://serpapi.com  (Google results; 1 credit per results page)
BRAVE_API_KEY=...       # https://api.search.brave.com  (1 request per results page)
ZENROWS_API_KEY=...     # https://www.zenrows.com  (only when fetch.renderer: zenrows)
```

ZenRows is used for rendering only, not search: its documented API fetches pages; it isn't a
search-results API. Searches go through SerpAPI and/or Brave.

`.env` is git-ignored. Keys are never written to logs, cache files or reports. They're redacted
if they ever appear in a log message.

### Configuration

`config.yaml` holds the search providers, results per query, delays, the per-run API call budget,
fetch politeness, screenshots, enrichment switches and paths. Every key is commented in the file.

| File | Purpose |
|---|---|
| `brokers.yaml` | Data-broker / people-search domains, opt-out URLs (verify before use), operator country. Extend freely. |
| `domain_categories.yaml` | Domain → Site Category mapping, `site:` query targets, suffix/keyword heuristics. |

Category names must match the tracker's **Lists** sheet exactly. The scanner checks every value it
might write against the dropdown lists in the tracker before a run starts, and stops with a clear
message if one is missing.

---

## Usage

```bash
# 1. See the query matrix (no network)
footprint-scanner queries --tracker Footprint_Removal_Tracker.xlsx

# 2. Dry run: queries + worst-case API cost, minus what's already cached. Spends nothing.
footprint-scanner scan --tracker Footprint_Removal_Tracker.xlsx --dry-run --providers serpapi,brave

# 3. Real scan (small first run)
footprint-scanner scan --tracker Footprint_Removal_Tracker.xlsx --max-queries 10

# Full scan with both providers merged
footprint-scanner scan --tracker Footprint_Removal_Tracker.xlsx --providers serpapi,brave

# 4. Monthly re-scan of the latest reviewed copy
footprint-scanner rescan --tracker clients/CASE-2026-001/Footprint_Removal_Tracker_2026-09-29_1430.xlsx

# Identifiers from a YAML file instead of the Client Profile sheet
footprint-scanner scan --tracker Footprint_Removal_Tracker.xlsx --client-yaml client.yaml
```

Other options: `--max-pages N` (pages fetched per run), `--no-screenshots`, `--verbose`,
`--config path/to/config.yaml`.

### Workflow: list → customer decides → you act

1. **Scan.** Findings go into a new tracker copy. Nothing is removed or requested.
2. **Review** the copy in Excel. Set Match Confidence to `Confirmed`, `Likely` or `Namesake (not client)`,
   and correct Severity or Removal Method where needed.
3. **Customer report.** Word is the recommended format: edit it in Word, then print it or
   save it as PDF from Word.

   ```bash
   footprint-scanner docx --tracker clients/CASE-2026-001/Footprint_Removal_Tracker_2026-09-29_1347.xlsx --lang tr --prepared-by "Vias Yazılım"
   ```

   The report contains:
   - a cover page with a summary by importance and site type;
   - a **findings list** (ID, site, type, information shown, importance, proposed action). Each ID
     is a link to that item's page, and the links keep working after Word's *Save as PDF*;
   - one page per finding: clickable link, site, what information is shown and where, the proposed
     action, and the screenshot;
   - **☐ Remove  ☐ Keep** tick boxes and a comment line on every item;
   - a final **Removal Authorization** page with name, signature and date lines.

   **Item numbers are the tracker's ID column,** so "Madde 6" is row ID 6 in Excel. Items follow
   the spreadsheet order; `--order severity` puts Critical items first and keeps the same numbers.

   Before sending, you can edit the text, remove an item, or add your logo in Word. If you delete
   an item page, also delete its row from the findings list. **Unmarked items mean no action.**

   Only `Confirmed` and `Likely` rows are included. Namesakes never are, and `Unverified` rows only
   with `--include-unverified`, because they may be other people's data. Your internal Notes are
   never included. The file is saved in `clients/<CASE-ID>/reports/`, which `purge` also deletes.

   `footprint-scanner pdf` (same options) builds a PDF directly instead, with fillable on-screen
   Remove / Keep fields.
4. **When the signed report comes back,** file the requests manually for the items marked Remove.
   Log the date and reference number in the tracker, and set items marked Keep to
   `No action needed`.

Set `report.prepared_by` and `report.lang` in `config.yaml` to avoid typing them each time.

### Authorization gate

`scan` and `rescan` refuse to run unless **Client Profile → "Signed authorization received?"** is
`Yes` (or `authorized: yes` in `client.yaml`). If you hold a signed authorization that isn't yet
recorded there, pass `--i-have-authorization`. The basis used (profile value or flag) is logged
and written to the run report. `queries` and `--dry-run` don't contact anything, so they aren't gated.

### Client identifiers

These are read from **Client Profile**, column B, with multiple values separated by `;`: full name,
name variants, DOB, cities, addresses, phones, emails, usernames, employers, schools, relatives,
and accounts to keep. Template placeholders such as `+90 5xx xxx xx xx` are skipped.
`client.yaml` uses the same fields:

```yaml
case_id: CASE-2026-001
full_name: Ahmet Yılmaz
name_variants: [Ahmet Yilmaz, A. Yılmaz, Ahmet Kemal Yılmaz]
dob: 1985-03-15
cities: [İstanbul, Ankara]
addresses: []
phones: ["+90 532 123 45 67"]
emails: []
usernames: [ahmetyilmaz34]
employers: []
schools: []
relatives: []
keep_accounts: [linkedin.com/in/ahmet-yilmaz]
authorized: yes
```

---

## What a run does

1. **Query matrix:**
   - Each name variant in quotes, both in Turkish spelling and ASCII (`ı/i, İ/I, ş/s, ğ/g, ü/u, ö/o, ç/c`).
   - Each name variant alone and combined with each city, employer and school.
   - Each email and username.
   - Each phone number in 7 formats (`+90 532 123 45 67`, `+905321234567`, `05321234567`,
     `0532 123 45 67`, `532-123-4567`, `(532) 123 45 67`, `532 123 45 67`).
   - `site:` queries for social and professional networks and every broker in `brokers.yaml`. These
     are grouped as `"name" (site:a OR site:b …)` to save credits; set
     `search.site_query_mode: per_domain` for one query per site.
   - Queries are deduplicated and ordered by value, so the most important ones run first when the
     budget is tight.
2. **Search:** SerpAPI and/or Brave, with pagination, exponential backoff on 429/5xx and a per-run
   API call budget. Raw responses are cached on disk, keyed by a hash of the query, so re-runs
   don't spend credits again.
3. **Per unique URL:**
   - **Fetching:** the URL is normalized (tracking parameters, fragments and trailing slashes
     removed). robots.txt is respected, with a per-domain delay and timeouts. Pages are fetched
     with httpx; the configured renderer (ZenRows or a local browser) renders JS-heavy pages and takes the full-page screenshot
     `evidence/<ID>_<domain>.png`.
   - **Analysis:** mentions are counted with Turkish-aware matching. The tool records where they
     appear and which data types are exposed.
   - **Enrichment:** RDAP supplies the owner and jurisdiction, and the Wayback Machine supplies
     archive status. The tool looks for removal/opt-out links, and assigns a category and the
     suggested values.
4. **Tracker copy:** written to `clients/<CASE-ID>/<tracker>_YYYY-MM-DD_HHMM.xlsx`. The input file
   is never modified.

### How the columns are filled

| Column | Rule |
|---|---|
| Google Rank for Name | Best position for the full legal name in quotes. Google (SerpAPI) is preferred; if only Brave found it, Notes says `Rank source: Brave (not Google)`. |
| Indexed in Google? | `Yes` if SerpAPI returned it, otherwise `Unknown`. |
| Mentions on Page | Name mentions in title + visible body + image alt text. Blank if the page couldn't be fetched. |
| Mentioned Places | Title, Meta description, Headings, Body, Image alt/captions, Comments section, URL slug (or "Search snippet" when not fetched). |
| Data Types Exposed | Phone, Email, Address, Date of birth, TC Kimlik No (checksum-validated), IBAN (mod-97), Photo, Relatives, Employer, School, City, Username. A value is flagged only if it matches a known identifier **or** is within ~300 characters of a name mention. A DOB also needs a birth keyword next to the date. |
| Site Owner / Jurisdiction | RDAP registrant (blank if redacted), IP-based hosting country, operator country from `brokers.yaml`, then a copyright line with a legal-entity suffix on the page or its imprint page. **Left blank rather than guessed.** |
| Site Category | `brokers.yaml`, then `domain_categories.yaml`, then suffix and keyword heuristics, else `Other`. |
| Client's Own Account? | `Yes` if a username is a URL path segment or subdomain on a social/professional/blog/forum/media site; `Unknown` for other pages on those sites; `No` otherwise. |
| Match Confidence | Always `Unverified`, unless the page has the name **plus** another known identifier (city, phone, email, employer, username, address, school, relative, DOB), in which case `Likely`. Never `Confirmed`. |
| Severity | Critical: ID number, IBAN, or address + photo. High: address, phone, DOB, email, or a data-broker profile. Medium: employer, photo, relatives, school, or a social/forum post. Low: name only. |
| Removal Method | Own account → *Client deletes own account*; broker with an opt-out → *Site opt-out form*; Turkish site (TR registrant/hosting or `.tr`) → *KVKK request (Law 6698)*; EU/EEA → *GDPR Art. 17 erasure request*; otherwise *Direct request to site owner*. |
| Deindex Fallback | *Google 'Results about you' / personal info form* when a phone, address, email, ID number or IBAN is exposed. |
| Removal Contact / Form URL | Opt-out URL from `brokers.yaml`, otherwise the best opt-out/removal/KVKK/privacy/contact link found on the page. |
| Status / dates | New rows: `Not started`, Date Found and Last Checked set to today. |
| Notes | Which provider/queries found the URL, why it wasn't fetched, keep-list matches. Full details are in `raw.jsonl`. |

The formula columns **ID, Follow-up Due and Days Since Request are never written**. Columns are
found by header name, so the template can be reordered. If row 2 is still the template's
`EXAMPLE ROW`, it's overwritten. If the sheet grows past row 1000, the formulas, dropdowns and
conditional formatting are extended. The Summary sheet still only counts rows 2–1000, and the
report warns about this.

### Re-scan rules

- Every existing row is re-fetched and its row is updated: Last Checked, Mentions on Page, Data
  Types Exposed, Google Rank for Name (cleared if no longer in the results), Indexed in Google?.
  Evidence is filled only if the cell is empty; new screenshots are saved as
  `evidence/<ID>_<domain>_<date>.png`.
- `Deindexed only` becomes **Re-appeared** if a search returns the URL again.
- `Removed` becomes **Re-appeared** if a search returns it again, or the page loads with HTTP 200
  and still names the client.
- For Re-appeared rows, a line is **appended** to Notes, for example
  `[scanner 2026-10-29] Re-appeared (was Removed): ...`. Existing note text is never edited.
- Match Confidence, other Status values, request dates and reference numbers are never changed.
- URLs that now return 404/410 are listed as **possibly removed** in the report. Their Status is
  not changed.
- New URLs are appended as `Not started` rows.

### Outputs

```
clients/<CASE-ID>/
  Footprint_Removal_Tracker_2026-09-29_1430.xlsx   # the updated copy
  evidence/0001_example.com.png                    # full-page screenshots
  cache/search/<provider>/<hash>.json              # raw API responses (credit saver)
  reports/CASE-2026-001_report_2026-10-01_0930_tr.docx # customer reports (.docx / .pdf)
  runs/2026-09-29_1430/
    report.md       # queries run, search + ZenRows credits used, new vs existing, re-appeared, possibly removed, errors
    raw.jsonl       # one JSON line per URL with everything extracted
    scan.log        # run log (no API keys)
```

---

## Data protection

Client identifiers, screenshots and search results are **personal data**. Treat the `clients/`
folder accordingly: keep it on an encrypted disk, and don't sync it to shared or cloud folders. It
is git-ignored.

- Everything for a client lives in `clients/<CASE-ID>/`. There is no global log with client data.
- **Where data goes:**
  - **Search APIs:** the client's identifiers (the queries) go only to the configured search APIs.
  - **Found sites:** pages that search returned are fetched like a normal browser visit, with the
    configured user agent.
  - **rdap.org:** receives only domain names and IP addresses. Switch off with `enrich.rdap: false`.
  - **archive.org:** receives the found URL, which can contain the name in its slug. Switch off
    with `enrich.wayback: false`.
  - **ZenRows** (only with `fetch.renderer: zenrows`): receives the URL of each robots-allowed page
    to screenshot or render, and returns the image/HTML. It doesn't receive the client's other
    identifiers. Use `renderer: playwright` with `browser_channel: msedge` to keep rendering
    entirely on your machine.
- **Delete everything for a client** (cached API responses, evidence, run reports/logs and tracker
  copies in the client folder):

  ```bash
  footprint-scanner purge --client CASE-2026-001          # asks for confirmation
  footprint-scanner purge --client CASE-2026-001 --yes    # no prompt
  ```

  `purge` doesn't touch your original input tracker or any copies you moved elsewhere. Delete those
  yourself by the retention date on Client Profile.

---

## Limitations

- Big social networks (Instagram, Facebook, LinkedIn, …) disallow crawlers in robots.txt. For
  those URLs the row is filled from the search snippet, with no screenshot, and Notes says so.
  Capture evidence for them manually while logged in as the reviewer.
- Detection is heuristic. False positives (namesakes) and misses are expected; that's why every
  row starts `Unverified` and is reviewed by a person.
- Removal-method suggestions are general categories, **not legal advice**. Have a lawyer review
  legal requests and court applications (KVKK, GDPR, Law 5651).

## Extending

- **A search backend:** add `src/footprint_scanner/search/<name>.py`, subclass `SearchProvider`,
  implement `build_request()` and `parse()`, and decorate the class with `@register("<name>")`.
  Import it in `make_providers()`, then list it under `search.providers` and read its key from `.env`
  (see `API_KEY_ENV` in `config.py`).
- **Brokers / categories:** edit `brokers.yaml` and `domain_categories.yaml`.

## Development

```bash
pytest -q           # all network calls are mocked
```

Tests cover query generation (Turkish transliteration, phone formats), TC Kimlik/IBAN checksums,
URL normalization, mention counting and data-type detection, classification rules, search
pagination/backoff/cache/budget, polite fetching, the Excel round-trip (formulas, validations and
conditional formatting preserved), re-scan merge rules, an end-to-end scan → review → rescan, and
the CLI authorization gate and `purge`.





footprint-scanner docx --tracker clients/CASE-2026-001/Footprint_Removal_Tracker_2026-09-29_1448.xlsx --lang tr --prepared-by "Vias Yazılım"
