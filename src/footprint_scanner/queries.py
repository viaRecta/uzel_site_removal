"""Build the search query matrix from client identifiers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .identifiers import ClientIdentifiers
from .sites import SiteDirectory
from .textnorm import collapse_ws, spelling_forms, tr_lower

# Lower number = run earlier, so the most valuable queries survive a tight budget.
KIND_PRIORITY = {
    "name_main": 0,
    "name": 1,
    "email": 2,
    "phone": 3,
    "username": 4,
    "name_city": 5,
    "name_employer": 6,
    "name_school": 7,
    "site_broker": 8,
    "site_social": 9,
    "site_professional": 10,
}


@dataclass(frozen=True)
class Query:
    text: str
    kind: str

    @property
    def is_main(self) -> bool:
        return self.kind == "name_main"

    @property
    def priority(self) -> int:
        return KIND_PRIORITY.get(self.kind, 99)


def quote(s: str) -> str:
    return '"' + s.replace('"', "").strip() + '"'


def turkish_national_number(raw: str) -> str | None:
    """10-digit Turkish national number ('5321234567') or None if it isn't one."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0090"):
        digits = digits[4:]
    elif digits.startswith("90") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "2345":
        return digits
    return None


def phone_key(raw: str) -> str:
    """Comparable key for a phone number: Turkish national digits, else the last 10 digits."""
    nat = turkish_national_number(raw)
    if nat:
        return nat
    digits = re.sub(r"\D", "", raw)
    return digits[-10:] if len(digits) >= 10 else digits


def phone_formats(raw: str) -> list[str]:
    """Common written forms of a phone number, e.g. for +90 532 123 45 67:
    '+90 532 123 45 67', '+905321234567', '05321234567', '0532 123 45 67',
    '532-123-4567', '(532) 123 45 67', '532 123 45 67'."""
    nat = turkish_national_number(raw)
    if not nat:
        digits = re.sub(r"\D", "", raw)
        forms = [raw.strip(), ("+" + digits) if raw.strip().startswith("+") else digits]
        return list(dict.fromkeys(forms))
    a, b, c, d = nat[:3], nat[3:6], nat[6:8], nat[8:]
    return list(
        dict.fromkeys(
            [
                f"+90 {a} {b} {c} {d}",
                f"+90{nat}",
                f"0{nat}",
                f"0{a} {b} {c} {d}",
                f"{a}-{b}-{c}{d}",
                f"({a}) {b} {c} {d}",
                f"{a} {b} {c} {d}",
            ]
        )
    )


def name_forms(ident: ClientIdentifiers) -> list[str]:
    """Every name and name variant, in original and ASCII-transliterated spelling."""
    forms: list[str] = []
    for n in ident.names:
        forms.extend(spelling_forms(n))
    return list(dict.fromkeys(forms))


def _site_groups(domains: list[str], mode: str, size: int) -> list[str]:
    domains = list(dict.fromkeys(d for d in domains if d))
    if mode == "per_domain" or size <= 1:
        return [f"site:{d}" for d in domains]
    groups = [domains[i : i + size] for i in range(0, len(domains), size)]
    return ["(" + " OR ".join(f"site:{d}" for d in g) + ")" if len(g) > 1 else f"site:{g[0]}" for g in groups]


def build_queries(
    ident: ClientIdentifiers,
    sites: SiteDirectory,
    site_query_mode: str = "grouped",
    site_group_size: int = 8,
) -> list[Query]:
    out: list[Query] = [Query(quote(ident.full_name), "name_main")]
    names = name_forms(ident)

    for n in names:
        out.append(Query(quote(n), "name"))
    for n in names:
        for kind, values in (
            ("name_city", ident.cities),
            ("name_employer", ident.employers),
            ("name_school", ident.schools),
        ):
            for v in values:
                out.append(Query(f"{quote(n)} {quote(v)}", kind))
    for e in ident.emails:
        out.append(Query(quote(e), "email"))
    for u in ident.usernames_clean:
        out.append(Query(quote(u), "username"))
    for p in ident.phones:
        for f in phone_formats(p):
            out.append(Query(quote(f), "phone"))

    site_sets = (
        ("site_broker", [b.domain for b in sites.brokers]),
        ("site_social", sites.social_query_sites),
        ("site_professional", sites.professional_query_sites),
    )
    for kind, domains in site_sets:
        for group in _site_groups(domains, site_query_mode, site_group_size):
            for n in names:
                out.append(Query(f"{quote(n)} {group}", kind))

    return dedupe(out)


def dedupe(queries: list[Query]) -> list[Query]:
    """Drop repeats (case/whitespace-insensitive, Turkish-aware), keep the higher-priority kind,
    and sort by priority."""
    best: dict[str, Query] = {}
    order: list[str] = []
    for q in queries:
        key = collapse_ws(tr_lower(q.text))
        if key not in best:
            best[key] = q
            order.append(key)
        elif q.priority < best[key].priority:
            best[key] = q
    result = [best[k] for k in order]
    result.sort(key=lambda q: q.priority)  # stable: keeps generation order within a kind
    return result
