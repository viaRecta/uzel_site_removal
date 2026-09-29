"""Read/write the Findings sheet of the Footprint Removal Tracker with openpyxl.

- Columns are located by header text in row 1, never by letter.
- Dropdown values are read from the sheet's own data validations (e.g. Lists!$A$2:$A$13),
  and every value is validated against them before it's written.
- Formula columns (ID, Follow-up Due, Days Since Request, or any column whose template row holds
  a formula) are never written.
- The input workbook is never saved over: save() refuses to write to the file it was loaded from.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import warnings
import zipfile
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import openpyxl
from lxml import etree
from openpyxl.formatting.formatting import ConditionalFormattingList
from openpyxl.formula.translate import Translator
from openpyxl.utils import get_column_letter, range_boundaries
from openpyxl.worksheet.cell_range import CellRange, MultiCellRange
from openpyxl.worksheet.datavalidation import DataValidation

from .urls import url_key

log = logging.getLogger(__name__)

FINDINGS = "Findings"

# Logical field -> header text in row 1 of Findings.
H_ID = "ID"
H_DATE_FOUND = "Date Found"
H_URL = "Site Link"
H_DOMAIN = "Domain"
H_OWNER = "Site Owner"
H_CATEGORY = "Site Category"
H_JURISDICTION = "Jurisdiction / Hosting Country"
H_CONFIDENCE = "Match Confidence"
H_OWN = "Client's Own Account?"
H_CONTENT = "Content Type"
H_DATA_TYPES = "Data Types Exposed"
H_PLACES = "Mentioned Places (on page)"
H_MENTIONS = "Mentions on Page"
H_RANK = "Google Rank for Name"
H_INDEXED = "Indexed in Google?"
H_ARCHIVED = "Archived Copies?"
H_SEVERITY = "Severity"
H_METHOD = "Removal Method"
H_CONTACT = "Removal Contact / Form URL"
H_FALLBACK = "Deindex Fallback"
H_SENT = "Request Sent Date"
H_REF = "Request Reference #"
H_DUE = "Follow-up Due"
H_DAYS = "Days Since Request"
H_STATUS = "Status"
H_CHECKED = "Last Checked"
H_EVIDENCE = "Evidence (screenshot path)"
H_NOTES = "Notes"

FORMULA_HEADERS = {H_ID, H_DUE, H_DAYS}
REQUIRED_HEADERS = {H_URL, H_STATUS, H_NOTES}
# Fields validated against the Removal Method list even though the column has no dropdown.
EXTRA_LIST_SOURCES = {H_FALLBACK: H_METHOD}
EXAMPLE_MARKER = "EXAMPLE ROW"

_TS_SUFFIX = re.compile(r"_\d{4}-\d{2}-\d{2}_\d{4}(?:\d{2})?$")


class TrackerError(Exception):
    pass


def _norm_header(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().casefold()


@dataclass
class ExistingRow:
    row: int
    url: str
    key: str
    values: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> int:
        return self.row - 1


_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "x14": "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main",
    "xm": "http://schemas.microsoft.com/office/excel/2006/main",
}
_DV_ATTRS = {  # x14 attribute -> openpyxl DataValidation keyword
    "type": "type", "operator": "operator", "allowBlank": "allow_blank", "errorStyle": "errorStyle",
    "showInputMessage": "showInputMessage", "showErrorMessage": "showErrorMessage",
    "errorTitle": "errorTitle", "error": "error", "promptTitle": "promptTitle", "prompt": "prompt",
    "showDropDown": "showDropDown",
}
_BOOL_ATTRS = {"allowBlank", "showInputMessage", "showErrorMessage", "showDropDown"}


def _x14_validations(path: Path) -> dict[str, list[DataValidation]]:
    """Data validations Excel stored in the x14 extension (extLst), per sheet name.

    Excel writes list validations that point at another sheet (e.g. Lists!$A$2:$A$13) in this
    form, and openpyxl drops them on load. They're rebuilt here as standard validations, which
    Excel 2010+ reads fine, so the output copy keeps its dropdowns."""
    out: dict[str, list[DataValidation]] = {}
    with zipfile.ZipFile(path) as z:
        wb = etree.fromstring(z.read("xl/workbook.xml"))
        rels = etree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.findall("rel:Relationship", _NS)}
        for sheet in wb.findall("main:sheets/main:sheet", _NS):
            target = targets.get(sheet.get(f"{{{_NS['r']}}}id"), "")
            part = target.lstrip("/") if target.startswith("/") else "xl/" + target
            if part not in z.namelist():
                continue
            root = etree.fromstring(z.read(part))
            for el in root.iterfind(".//x14:dataValidation", _NS):
                kwargs: dict[str, Any] = {}
                for attr, kw in _DV_ATTRS.items():
                    v = el.get(attr)
                    if v is not None:
                        kwargs[kw] = v in ("1", "true") if attr in _BOOL_ATTRS else v
                f1 = el.findtext("x14:formula1/xm:f", namespaces=_NS)
                f2 = el.findtext("x14:formula2/xm:f", namespaces=_NS)
                sqref = el.findtext("xm:sqref", namespaces=_NS)
                if not sqref or f1 is None:
                    continue
                dv = DataValidation(formula1=f1, **kwargs)
                if f2 is not None and kwargs.get("type") != "list":
                    dv.formula2 = f2
                dv.sqref = MultiCellRange(sqref)
                out.setdefault(sheet.get("name"), []).append(dv)
    return out


def load_workbook_preserving_validations(path: Path, **kw: Any) -> openpyxl.Workbook:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Data Validation extension is not supported")
        wb = openpyxl.load_workbook(path, **kw)
    if kw.get("read_only"):
        return wb
    restored = _x14_validations(path)
    for name, dvs in restored.items():
        if name in wb.sheetnames:
            for dv in dvs:
                wb[name].add_data_validation(dv)
    if restored:
        log.info("Restored %d dropdown validation(s) Excel stored in extended format",
                 sum(len(v) for v in restored.values()))
    return wb


def timestamped_name(input_path: Path, when: dt.datetime) -> str:
    stem = _TS_SUFFIX.sub("", input_path.stem)
    return f"{stem}_{when:%Y-%m-%d_%H%M}.xlsx"


class Tracker:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.wb = load_workbook_preserving_validations(self.path)
        if FINDINGS not in self.wb.sheetnames:
            raise TrackerError(f"Sheet '{FINDINGS}' not found")
        self.ws = self.wb[FINDINGS]
        self.columns: dict[str, int] = {}
        self.header_text: dict[str, str] = {}
        for cell in self.ws[1]:
            if cell.value not in (None, ""):
                key = _norm_header(cell.value)
                self.columns[key] = cell.column
                self.header_text[key] = str(cell.value).strip()
        missing = [h for h in REQUIRED_HEADERS if _norm_header(h) not in self.columns]
        if missing:
            raise TrackerError(f"Findings is missing required column(s): {', '.join(missing)}")
        self.template_row = 2
        self.formula_columns = self._formula_columns()
        self.allowed = self._allowed_values()
        self.formula_extent = self._formula_extent()

    # ------------------------------------------------------------ structure

    def col(self, header: str) -> int | None:
        return self.columns.get(_norm_header(header))

    def has(self, header: str) -> bool:
        return self.col(header) is not None

    def _formula_columns(self) -> set[int]:
        cols = {c for h in FORMULA_HEADERS if (c := self.col(h))}
        for r in (2, 3):
            for cell in self.ws[r]:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    cols.add(cell.column)
        return cols

    def _formula_extent(self) -> int:
        """Last row that already has the template formulas filled in."""
        if not self.formula_columns:
            return self.ws.max_row
        c = min(self.formula_columns)
        last = 1
        for r in range(2, self.ws.max_row + 1):
            v = self.ws.cell(r, c).value
            if isinstance(v, str) and v.startswith("="):
                last = r
        return last

    def _list_values(self, formula: str) -> list[str] | None:
        f = formula.strip()
        if f.startswith('"') and f.endswith('"'):
            return [x.strip() for x in f.strip('"').split(",") if x.strip()]
        m = re.fullmatch(r"=?'?([^'!]+)'?!\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)", f)
        if not m:
            return None
        sheet, c1, r1, c2, r2 = m.groups()
        if sheet not in self.wb.sheetnames:
            return None
        src = self.wb[sheet]
        out = []
        for row in src[f"{c1}{r1}:{c2}{r2}"]:
            for cell in row:
                if cell.value not in (None, ""):
                    out.append(str(cell.value).strip())
        return out

    def _allowed_values(self) -> dict[int, list[str]]:
        allowed: dict[int, list[str]] = {}
        for dv in self.ws.data_validations.dataValidation:
            if dv.type != "list" or not dv.formula1:
                continue
            values = self._list_values(dv.formula1)
            if values is None:
                continue
            for rng in dv.sqref.ranges:
                for c in range(rng.min_col, rng.max_col + 1):
                    allowed[c] = values
        for field_header, source_header in EXTRA_LIST_SOURCES.items():
            fc, sc = self.col(field_header), self.col(source_header)
            if fc and sc and sc in allowed and fc not in allowed:
                allowed[fc] = allowed[sc]
        return allowed

    def allowed_for(self, header: str) -> list[str] | None:
        c = self.col(header)
        return self.allowed.get(c) if c else None

    def validate(self, header: str, value: Any) -> None:
        if value in (None, ""):
            return
        allowed = self.allowed_for(header)
        if allowed is not None and value not in allowed:
            raise TrackerError(f"Value {value!r} is not in the Lists options for '{header}': {allowed}")

    def check_values_exist(self, header: str, values: set[str]) -> list[str]:
        """Values the scanner may emit that the template's list doesn't contain."""
        allowed = self.allowed_for(header)
        return sorted(values - set(allowed)) if allowed is not None else []

    # ------------------------------------------------------------ rows

    def get(self, row: int, header: str) -> Any:
        c = self.col(header)
        return self.ws.cell(row, c).value if c else None

    def set(self, row: int, header: str, value: Any) -> bool:
        """Write one cell. Returns False (and writes nothing) for missing or formula columns."""
        c = self.col(header)
        if c is None:
            return False
        if c in self.formula_columns:
            raise TrackerError(f"Refusing to write formula column '{header}'")
        self.validate(header, value)
        if isinstance(value, dt.datetime):
            value = value.replace(tzinfo=None)
        elif isinstance(value, dt.date):
            value = dt.datetime(value.year, value.month, value.day)
        self.ws.cell(row, c).value = None if value == "" else value
        return True

    def is_example_row(self, row: int = 2) -> bool:
        notes = self.get(row, H_NOTES)
        return isinstance(notes, str) and notes.strip().upper().startswith(EXAMPLE_MARKER)

    def clear_row(self, row: int) -> None:
        for c in self.columns.values():
            if c not in self.formula_columns:
                self.ws.cell(row, c).value = None

    def clear_example_row(self) -> bool:
        if self.is_example_row(2):
            self.clear_row(2)
            return True
        return False

    def _last_used_row(self) -> int:
        c = self.col(H_URL)
        last = 1
        for r in range(2, self.ws.max_row + 1):
            if self.ws.cell(r, c).value not in (None, ""):
                last = r
        return last

    def existing_rows(self) -> list[ExistingRow]:
        c = self.col(H_URL)
        rows = []
        for r in range(2, self._last_used_row() + 1):
            url = self.ws.cell(r, c).value
            if url in (None, "") or (r == 2 and self.is_example_row(2)):
                continue
            values = {self.header_text[k]: self.ws.cell(r, col).value for k, col in self.columns.items()}
            rows.append(ExistingRow(row=r, url=str(url).strip(), key=url_key(str(url)), values=values))
        return rows

    def allocate_rows(self, n: int) -> list[int]:
        """Row numbers for n new findings, appended after the last used row."""
        start = self._last_used_row() + 1
        rows = list(range(start, start + n))
        if rows:
            self.ensure_rows(rows[-1])
        return rows

    def append_note(self, row: int, line: str) -> None:
        """Add a line to Notes without altering any existing text."""
        current = self.get(row, H_NOTES)
        text = str(current).rstrip() if current not in (None, "") else ""
        self.set(row, H_NOTES, f"{text}\n{line}" if text else line)

    # ------------------------------------------------------------ growth beyond the template

    def ensure_rows(self, last_row: int) -> bool:
        """Copy formulas/styles down and extend validations, conditional formatting and the
        autofilter so rows up to `last_row` behave like template rows. Returns True if extended."""
        if last_row <= self.formula_extent:
            return False
        old_extent = self.formula_extent
        src = self.template_row
        for r in range(old_extent + 1, last_row + 1):
            for c in range(1, self.ws.max_column + 1):
                s, d = self.ws.cell(src, c), self.ws.cell(r, c)
                if s.has_style:
                    d._style = copy(s._style)
                if c in self.formula_columns and isinstance(s.value, str) and s.value.startswith("="):
                    d.value = Translator(s.value, origin=s.coordinate).translate_formula(d.coordinate)
        self._extend_ranges(old_extent, last_row)
        self.formula_extent = last_row
        log.warning(
            "Findings grew past row %d; formulas and formatting were extended to row %d. "
            "Summary sheet formulas only count up to row %d.", old_extent, last_row, old_extent,
        )
        return True

    def _extend_multi(self, sqref: MultiCellRange, old_max: int, new_max: int) -> MultiCellRange:
        out = []
        for rng in sqref.ranges:
            if rng.max_row == old_max:
                rng = CellRange(min_col=rng.min_col, min_row=rng.min_row, max_col=rng.max_col, max_row=new_max)
            out.append(rng.coord)
        return MultiCellRange(" ".join(out))

    def _extend_ranges(self, old_max: int, new_max: int) -> None:
        for dv in self.ws.data_validations.dataValidation:
            dv.sqref = self._extend_multi(dv.sqref, old_max, new_max)
        old_cf = self.ws.conditional_formatting
        new_cf = ConditionalFormattingList()
        for cf in old_cf:
            rng = self._extend_multi(cf.sqref, old_max, new_max)
            for rule in cf.rules:
                new_cf.add(str(rng), rule)
        self.ws.conditional_formatting = new_cf
        if self.ws.auto_filter.ref:
            min_col, min_row, max_col, max_row = range_boundaries(self.ws.auto_filter.ref)
            if max_row == old_max:
                self.ws.auto_filter.ref = (
                    f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{new_max}"
                )

    # ------------------------------------------------------------ save

    def save(self, out_path: str | Path) -> Path:
        out = Path(out_path).resolve()
        if out == self.path:
            raise TrackerError("Refusing to overwrite the input tracker; save to a new copy.")
        out.parent.mkdir(parents=True, exist_ok=True)
        self.wb.save(out)
        return out
