"""Page analysis: name mentions, where they occur, exposed data types, removal/owner hints.

All matching runs on folded text (see textnorm.fold), so positions are consistent across
detectors and 'Yılmaz' / 'YILMAZ' / 'Yilmaz' are treated the same.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .identifiers import ClientIdentifiers
from .queries import phone_key, turkish_national_number
from .textnorm import collapse_ws, contains_phrase, find_spans, fold, spelling_forms

PROXIMITY = 300  # chars: an unknown value only counts if this close to a name mention

# Mentioned Places labels, in the order they're reported.
PLACE_TITLE = "Title"
PLACE_META = "Meta description"
PLACE_HEADINGS = "Headings"
PLACE_BODY = "Body"
PLACE_IMAGES = "Image alt/captions"
PLACE_COMMENTS = "Comments section"
PLACE_URL = "URL slug"
PLACE_SNIPPET = "Search snippet"
PLACE_ORDER = [PLACE_TITLE, PLACE_META, PLACE_HEADINGS, PLACE_BODY, PLACE_IMAGES, PLACE_COMMENTS, PLACE_URL, PLACE_SNIPPET]

# Data Types Exposed labels, in the order they're reported.
DT_NAME = "Full name"
DT_PHONE = "Phone"
DT_EMAIL = "Email"
DT_ADDRESS = "Address"
DT_DOB = "Date of birth"
DT_TCKN = "TC Kimlik No"
DT_IBAN = "IBAN / financial"
DT_PHOTO = "Photo"
DT_RELATIVES = "Relatives"
DT_EMPLOYER = "Employer"
DT_SCHOOL = "School"
DT_CITY = "City"
DT_USERNAME = "Username"
DT_ORDER = [
    DT_NAME, DT_PHONE, DT_EMAIL, DT_ADDRESS, DT_DOB, DT_TCKN, DT_IBAN, DT_PHOTO,
    DT_RELATIVES, DT_EMPLOYER, DT_SCHOOL, DT_CITY, DT_USERNAME,
]

_STRIP_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "head")
_COMMENT_HINT = re.compile(r"comment|yorum|disqus|reply|respond|yanit|cevap", re.I)

_PHONE_RE = re.compile(r"(?<![\w+])(?:\+|00)?\(?\d[\d\s().\-]{7,17}\d(?!\w)")
_EMAIL_RE = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+")
_TCKN_RE = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")
_IBAN_RE = re.compile(r"(?<![a-z0-9])[a-z]{2}\d{2}(?:\s?[a-z0-9]){11,30}(?![a-z0-9])")
_ADDRESS_RE = re.compile(
    r"(?<!\w)(?:mahallesi|mah\.|mh\.|caddesi|cad\.|cd\.|sokagi|sokak|sok\.|sk\.|bulvari|blv\.|"
    r"no\s*:\s*\d+|daire\s*:?\s*\d+|kat\s*:\s*\d+|apt\.|apartmani|sitesi|"
    r"street|avenue|ave\.|road|rd\.|suite|p\.?o\.? box)(?!\w)"
)
_DATE_RE = re.compile(
    r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d{2}|(?:19|20)\d{2}-\d{2}-\d{2}|"
    r"\d{1,2}\s+(?:ocak|subat|mart|nisan|mayis|haziran|temmuz|agustos|eylul|ekim|kasim|aralik|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)\s+(?:19|20)\d{2})(?!\d)"
)
_BIRTH_RE = re.compile(r"dogum|d\.\s?t\.|born|birth|dob|d\.o\.b|birthday")

_MONTHS_TR = ["ocak", "subat", "mart", "nisan", "mayis", "haziran", "temmuz", "agustos", "eylul", "ekim", "kasim", "aralik"]
_MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]

# (pattern on folded link text + href, score). Highest score wins.
_REMOVAL_HINTS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"opt[\s\-_]?out"), 10),
    (re.compile(r"remove (?:my )?(?:info|information|listing|profile|record|data)"), 10),
    (re.compile(r"icerik kaldirma|kaldirma talebi|kaldirma"), 9),
    (re.compile(r"removal|suppression"), 9),
    (re.compile(r"do[\s\-_]?not[\s\-_]?sell"), 8),
    (re.compile(r"veri sahibi|basvuru formu|ilgili kisi"), 8),
    (re.compile(r"kvkk|kisisel veri"), 7),
    (re.compile(r"gdpr|data request|erasure|delete my"), 7),
    (re.compile(r"privacy|gizlilik|aydinlatma"), 5),
    (re.compile(r"contact|iletisim|bize ulasin|impressum|imprint|kunye"), 3),
]
_IMPRINT_HINT = re.compile(r"impressum|imprint|kunye|hakkimizda|about|legal|yasal")
_ORG_SUFFIX = (
    r"(?:Ltd\.?\s*Şti\.?|Ltd\.?|Limited|Inc\.?|LLC|L\.L\.C\.|GmbH|A\.Ş\.|A\.S\.|Şirketi|Corp\.?|"
    r"Corporation|S\.A\.|B\.V\.|AG|PLC|Holding|Yayıncılık|Medya)"
)
_OWNER_RE = re.compile(
    r"(?:©|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*[,.]?\s*"
    r"([A-Za-z0-9ÇĞİÖŞÜçğıöşü][^\n|•·©]{1,80}" + _ORG_SUFFIX + ")",
    re.IGNORECASE,
)


@dataclass
class PageAnalysis:
    mentions: int = 0
    places: list[str] = field(default_factory=list)
    data_types: list[str] = field(default_factory=list)
    matched: dict[str, list[str]] = field(default_factory=dict)  # known identifiers found
    removal_links: list[dict[str, object]] = field(default_factory=list)
    imprint_links: list[str] = field(default_factory=list)
    owner_hint: str = ""
    title: str = ""
    visible_text_len: int = 0
    contexts: list[str] = field(default_factory=list)
    source: str = "page"  # "page" or "snippet"

    @property
    def best_removal_link(self) -> str:
        return str(self.removal_links[0]["url"]) if self.removal_links else ""

    @property
    def has_opt_out_link(self) -> bool:
        return bool(self.removal_links) and int(self.removal_links[0]["score"]) >= 8


# ---------------------------------------------------------------- validators


def tckn_valid(s: str) -> bool:
    """TC Kimlik No checksum: 11 digits, first non-zero,
    d10 = ((d1+d3+d5+d7+d9)*7 - (d2+d4+d6+d8)) mod 10, d11 = sum(d1..d10) mod 10."""
    if not re.fullmatch(r"[1-9]\d{10}", s):
        return False
    d = [int(c) for c in s]
    if ((sum(d[0:9:2]) * 7) - sum(d[1:8:2])) % 10 != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]


def iban_valid(s: str) -> bool:
    s = re.sub(r"\s", "", s).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    if s.startswith("TR") and len(s) != 26:
        return False
    rearranged = s[4:] + s[:4]
    return int("".join(str(int(ch, 36)) for ch in rearranged)) % 97 == 1


# ---------------------------------------------------------------- helpers


def _near(pos: int, spans: list[tuple[int, int]], distance: int = PROXIMITY) -> bool:
    return any(start - distance <= pos <= end + distance for start, end in spans)


def _dob_strings(dob: dt.date) -> list[str]:
    d, m, y = dob.day, dob.month, dob.year
    out = []
    for sep in ".", "/", "-":
        out += [f"{d:02d}{sep}{m:02d}{sep}{y}", f"{d}{sep}{m}{sep}{y}"]
    out += [f"{y}-{m:02d}-{d:02d}", f"{d} {_MONTHS_TR[m - 1]} {y}", f"{d} {_MONTHS_EN[m - 1]} {y}"]
    return out


def _slug_text(url: str) -> str:
    parts = urlsplit(url)
    return re.sub(r"[/_\-+.=&?]+", " ", unquote(parts.path + " " + parts.query))


def _names(ident: ClientIdentifiers) -> list[str]:
    out: list[str] = []
    for n in ident.names:
        out.extend(spelling_forms(n))
    return out


def _known_phrase_hits(folded: str, values: list[str]) -> list[str]:
    return [v for v in values if contains_phrase(folded, v)]


def _address_hits(folded: str, addresses: list[str]) -> list[str]:
    hits = []
    for a in addresses:
        parts = [p for p in re.split(r"[,;]", a) if len(p.strip()) >= 8] or [a]
        if contains_phrase(folded, a) or any(contains_phrase(folded, p) for p in parts):
            hits.append(a)
    return hits


def _is_icon(img: Tag) -> bool:
    src = fold(str(img.get("src") or ""))
    if re.search(r"logo|icon|sprite|avatar-default|placeholder|pixel|spacer|badge", src):
        return True
    for attr in ("width", "height"):
        v = str(img.get(attr) or "")
        if v.isdigit() and int(v) < 48:
            return True
    return False


def _photo_near_name(soup: BeautifulSoup, names: list[str]) -> bool:
    for img in soup.find_all("img"):
        if _is_icon(img):
            continue
        label = fold(" ".join(str(img.get(a) or "") for a in ("alt", "title")))
        if any(contains_phrase(label, n) for n in names):
            return True
        node = img.parent
        for _ in range(3):
            if node is None or not isinstance(node, Tag):
                break
            text = fold(node.get_text(" ", strip=True))
            if len(text) > 600:
                break
            if any(contains_phrase(text, n) for n in names):
                return True
            node = node.parent
    return False


def _removal_links(soup: BeautifulSoup, base_url: str) -> tuple[list[dict[str, object]], list[str]]:
    scored: dict[str, dict[str, object]] = {}
    imprint: list[str] = []
    base_host = urlsplit(base_url).hostname or ""
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        absolute = href if href.startswith("mailto:") else urljoin(base_url, href)
        hay = fold(a.get_text(" ", strip=True) + " " + href)
        score = max((s for pat, s in _REMOVAL_HINTS if pat.search(hay)), default=0)
        if score and (absolute not in scored or int(scored[absolute]["score"]) < score):
            scored[absolute] = {"url": absolute, "text": collapse_ws(a.get_text(" ", strip=True))[:80], "score": score}
        if _IMPRINT_HINT.search(hay) and (urlsplit(absolute).hostname or "") == base_host:
            imprint.append(absolute)
    ranked = sorted(scored.values(), key=lambda x: -int(x["score"]))  # stable: page order breaks ties
    return ranked[:5], list(dict.fromkeys(imprint))[:3]


def owner_from_text(text: str) -> str:
    """Company named in a copyright line (only when it has a legal-entity suffix)."""
    m = _OWNER_RE.search(text)
    if not m:
        return ""
    owner = collapse_ws(m.group(1)).strip(" .,-")
    owner = re.sub(r"^(?:all rights reserved|tüm hakları saklıdır)\W*", "", owner, flags=re.I)
    return owner[:100]


# ---------------------------------------------------------------- core


def _detect(
    ident: ClientIdentifiers,
    folded: str,
    spans: list[tuple[int, int]],
) -> tuple[set[str], dict[str, list[str]]]:
    types: set[str] = set()
    matched: dict[str, list[str]] = {}
    if spans:
        types.add(DT_NAME)

    # Phones
    known_phones = {phone_key(p): p for p in ident.phones}
    for m in _PHONE_RE.finditer(folded):
        raw = m.group(0)
        digits = re.sub(r"\D", "", raw)
        if not 10 <= len(digits) <= 13:
            continue
        key = phone_key(raw)
        if key in known_phones:
            types.add(DT_PHONE)
            matched.setdefault("phone", []).append(known_phones[key])
        elif (turkish_national_number(raw) or raw.startswith(("+", "00"))) and _near(m.start(), spans):
            types.add(DT_PHONE)

    # Emails
    known_emails = {e.lower() for e in ident.emails}
    for m in _EMAIL_RE.finditer(folded):
        e = m.group(0).strip(".")
        if e in known_emails:
            types.add(DT_EMAIL)
            matched.setdefault("email", []).append(e)
        elif _near(m.start(), spans):
            types.add(DT_EMAIL)

    # Addresses
    if hits := _address_hits(folded, ident.addresses):
        types.add(DT_ADDRESS)
        matched["address"] = hits
    else:
        for m in _ADDRESS_RE.finditer(folded):
            window = folded[max(0, m.start() - 40) : m.end() + 40]
            if re.search(r"\d", window) and _near(m.start(), spans):
                types.add(DT_ADDRESS)
                break

    # Date of birth: the known DOB anywhere, or a date next to a birth keyword near the name.
    if ident.dob and any(s in folded for s in _dob_strings(ident.dob)):
        types.add(DT_DOB)
        matched["dob"] = [ident.dob.isoformat()]
    else:
        for m in _DATE_RE.finditer(folded):
            window = folded[max(0, m.start() - 60) : m.end() + 20]
            if _BIRTH_RE.search(window) and _near(m.start(), spans):
                types.add(DT_DOB)
                break

    # TC Kimlik: checksum-valid 11-digit numbers near the name.
    if any(tckn_valid(m.group(0)) and _near(m.start(), spans) for m in _TCKN_RE.finditer(folded)):
        types.add(DT_TCKN)

    # IBAN: mod-97-valid near the name.
    if any(iban_valid(m.group(0)) and _near(m.start(), spans) for m in _IBAN_RE.finditer(folded)):
        types.add(DT_IBAN)

    # Known-only identifiers.
    for kind, label, values in (
        ("relative", DT_RELATIVES, [f for r in ident.relatives for f in spelling_forms(r)]),
        ("employer", DT_EMPLOYER, ident.employers),
        ("school", DT_SCHOOL, ident.schools),
        ("city", DT_CITY, ident.cities),
        ("username", DT_USERNAME, ident.usernames_clean),
    ):
        if hits := _known_phrase_hits(folded, values):
            types.add(label)
            matched[kind] = list(dict.fromkeys(hits))

    return types, {k: list(dict.fromkeys(v)) for k, v in matched.items()}


def _contexts(folded: str, spans: list[tuple[int, int]], n: int = 5, width: int = 80) -> list[str]:
    return [collapse_ws(folded[max(0, s - width) : e + width]) for s, e in spans[:n]]


def _sorted(items: set[str], order: list[str]) -> list[str]:
    return [x for x in order if x in items]


def analyze_html(html: str, url: str, ident: ClientIdentifiers) -> PageAnalysis:
    soup = BeautifulSoup(html, "lxml")
    names = _names(ident)

    title = collapse_ws(soup.title.get_text()) if soup.title else ""
    meta_parts = []
    for attrs in ({"name": "description"}, {"property": "og:description"}, {"name": "twitter:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            meta_parts.append(str(tag["content"]))
    meta = collapse_ws(" ".join(meta_parts))

    removal, imprint = _removal_links(soup, url)
    photo = _photo_near_name(soup, names)

    for t in soup.find_all(_STRIP_TAGS):
        t.decompose()
    body = soup.body or soup
    alts = collapse_ws(" ".join(str(i.get("alt") or "") for i in body.find_all("img")))
    captions = collapse_ws(" ".join(c.get_text(" ") for c in body.find_all("figcaption")))
    headings = collapse_ws(" ".join(h.get_text(" ") for h in body.find_all(re.compile(r"^h[1-6]$"))))

    comment_nodes = [
        el for el in body.find_all(True)
        if isinstance(el, Tag) and el.attrs is not None
        and _COMMENT_HINT.search(" ".join([str(el.get("id") or ""), *[str(c) for c in (el.get("class") or [])]]))
    ]
    # Keep only outermost comment containers.
    comment_nodes = [el for el in comment_nodes if not any(p in comment_nodes for p in el.parents)]
    comments = collapse_ws(" ".join(el.get_text(" ") for el in comment_nodes))

    full_body = collapse_ws(body.get_text(" "))
    for el in comment_nodes:
        el.decompose()
    for h in body.find_all(re.compile(r"^h[1-6]$")):
        h.decompose()
    plain_body = collapse_ws(body.get_text(" "))

    places = set()
    for place, text in (
        (PLACE_TITLE, title),
        (PLACE_META, meta),
        (PLACE_HEADINGS, headings),
        (PLACE_BODY, plain_body),
        (PLACE_IMAGES, alts + " " + captions),
        (PLACE_COMMENTS, comments),
        (PLACE_URL, _slug_text(url)),
    ):
        if text and find_spans(fold(text), names):
            places.add(place)

    # Mentions: title + visible body (headings, captions and comments included) + image alt text.
    counted = fold("\n".join([title, full_body, alts]))
    spans = find_spans(counted, names)

    # Detection text also includes meta description and the URL (usernames often live there).
    detect_text = fold("\n".join([title, meta, full_body, alts, _slug_text(url)]))
    detect_spans = find_spans(detect_text, names)
    types, matched = _detect(ident, detect_text, detect_spans)
    if photo:
        types.add(DT_PHOTO)

    return PageAnalysis(
        mentions=len(spans),
        places=_sorted(places, PLACE_ORDER),
        data_types=_sorted(types, DT_ORDER),
        matched=matched,
        removal_links=removal,
        imprint_links=imprint,
        owner_hint=owner_from_text(full_body[-3000:]),
        title=title,
        visible_text_len=len(full_body),
        contexts=_contexts(counted, spans),
        source="page",
    )


def analyze_snippet(title: str, snippet: str, url: str, ident: ClientIdentifiers) -> PageAnalysis:
    """Fallback when the page couldn't be fetched: analyze the search result title + snippet."""
    names = _names(ident)
    text = fold("\n".join([title, snippet, _slug_text(url)]))
    spans = find_spans(text, names)
    types, matched = _detect(ident, text, spans)
    places = set()
    if find_spans(fold(title + " " + snippet), names):
        places.add(PLACE_SNIPPET)
    if find_spans(fold(_slug_text(url)), names):
        places.add(PLACE_URL)
    return PageAnalysis(
        mentions=0,
        places=_sorted(places, PLACE_ORDER),
        data_types=_sorted(types, DT_ORDER),
        matched=matched,
        title=title,
        contexts=_contexts(text, spans),
        source="snippet",
    )


def match_confidence(analysis: PageAnalysis) -> str:
    """'Likely' only when the name AND at least one other known identifier appear; else 'Unverified'.
    Never 'Confirmed' - that is set by a human after review."""
    has_name = DT_NAME in analysis.data_types
    return "Likely" if has_name and analysis.matched else "Unverified"
