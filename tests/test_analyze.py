import datetime as dt

import pytest

from footprint_scanner.analyze import (
    DT_ADDRESS,
    DT_CITY,
    DT_DOB,
    DT_EMAIL,
    DT_EMPLOYER,
    DT_IBAN,
    DT_NAME,
    DT_PHONE,
    DT_PHOTO,
    DT_RELATIVES,
    DT_TCKN,
    DT_USERNAME,
    analyze_html,
    analyze_snippet,
    iban_valid,
    match_confidence,
    owner_from_text,
    tckn_valid,
)

FILLER = "<p>" + ("lorem ipsum dolor sit amet " * 30) + "</p>"  # ~800 chars


def make_tckn(first9: str) -> str:
    d = [int(c) for c in first9]
    d10 = ((sum(d[0:9:2]) * 7) - sum(d[1:8:2])) % 10
    d11 = (sum(d) + d10) % 10
    return first9 + str(d10) + str(d11)


@pytest.mark.parametrize("n", ["10000000146", make_tckn("123456789"), make_tckn("987654321")])
def test_tckn_valid(n):
    assert tckn_valid(n)


@pytest.mark.parametrize("n", ["10000000147", "01234567890", "1234567890", "123456789012", "abcdefghijk"])
def test_tckn_invalid(n):
    assert not tckn_valid(n)


def test_iban():
    assert iban_valid("TR33 0006 1005 1978 6457 8413 26")
    assert iban_valid("GB82WEST12345698765432")
    assert not iban_valid("TR33 0006 1005 1978 6457 8413 27")


def test_mentions_and_places(ident):
    html = """<html><head><title>Ahmet Yılmaz - Profil</title>
    <meta name="description" content="AHMET YILMAZ hakkında bilgi"></head>
    <body><h1>Ahmet Yilmaz</h1><p>Bu sayfa ahmet yılmaz ile ilgili.</p>
    <img src="/p/1.jpg" alt="Ahmet Kemal Yılmaz portresi">
    <div id="comments"><p>A. Yılmaz çok iyi biri</p></div>
    <script>var x = "Ahmet Yılmaz";</script></body></html>"""
    a = analyze_html(html, "https://example.com/kisi/ahmet-yilmaz", ident)
    # title + h1 + paragraph + comment + alt = 5; meta and script are excluded from the count
    assert a.mentions == 5
    assert a.places == ["Title", "Meta description", "Headings", "Body", "Image alt/captions", "Comments section", "URL slug"]
    assert DT_PHOTO in a.data_types


def test_known_identifiers_flagged_anywhere(ident):
    html = f"""<body><p>Ahmet Yılmaz</p>{FILLER}{FILLER}
    <p>Tel: 0532 123 45 67, ahmet.yilmaz@example.com, Acme Yazılım, İstanbul, Ayşe Yilmaz,
    Bağdat Caddesi No: 12 Kadıköy, @ahmetyilmaz34</p></body>"""
    a = analyze_html(html, "https://x.com/a", ident)
    for t in (DT_NAME, DT_PHONE, DT_EMAIL, DT_EMPLOYER, DT_CITY, DT_RELATIVES, DT_ADDRESS, DT_USERNAME):
        assert t in a.data_types, t
    assert a.matched["phone"] == ["+90 532 123 45 67"]
    assert match_confidence(a) == "Likely"


def test_unknown_values_only_near_name(ident):
    tckn = make_tckn("123456789")
    far = f"<body><p>Ahmet Yılmaz</p>{FILLER}<p>Tel +90 212 555 11 22 mail info@other.com TC {tckn}</p></body>"
    a = analyze_html(far, "https://x.com/a", ident)
    assert DT_PHONE not in a.data_types and DT_EMAIL not in a.data_types and DT_TCKN not in a.data_types
    assert match_confidence(a) == "Unverified"

    near = f"<body><p>Ahmet Yılmaz, Tel +90 212 555 11 22, info@other.com, TC {tckn}, IBAN TR33 0006 1005 1978 6457 8413 26</p></body>"
    a = analyze_html(near, "https://x.com/a", ident)
    assert {DT_PHONE, DT_EMAIL, DT_TCKN, DT_IBAN} <= set(a.data_types)
    # Unknown values near the name don't make a match 'Likely' on their own.
    assert match_confidence(a) == "Unverified"


def test_invalid_tckn_near_name_not_flagged(ident):
    a = analyze_html("<body><p>Ahmet Yılmaz 12345678901</p></body>", "https://x.com", ident)
    assert DT_TCKN not in a.data_types


def test_dob_detection(ident):
    a = analyze_html("<body><p>Ahmet Yılmaz, doğum tarihi: 15.03.1985</p></body>", "https://x.com", ident)
    assert DT_DOB in a.data_types
    # A plain date (article date) is not a DOB.
    a = analyze_html("<body><p>Ahmet Yılmaz 15.03.2024 tarihinde açıklama yaptı</p></body>", "https://x.com", ident)
    assert DT_DOB not in a.data_types
    ident.dob = dt.date(1985, 3, 15)
    a = analyze_html(f"<body><p>Ahmet Yılmaz</p>{FILLER}<p>1985-03-15</p></body>", "https://x.com", ident)
    assert DT_DOB in a.data_types and a.matched["dob"] == ["1985-03-15"]


def test_no_name_means_no_proximity_flags(ident):
    a = analyze_html("<body><p>Tel +90 212 555 11 22 Bağdat Cad. No: 5</p></body>", "https://x.com", ident)
    assert a.mentions == 0 and a.data_types == []


def test_removal_links_ranked(ident):
    html = """<body><a href="/contact">İletişim</a><a href="/privacy">Gizlilik Politikası</a>
    <a href="https://example.com/optout">Opt out of this site</a><a href="mailto:kvkk@x.com">KVKK</a></body>"""
    a = analyze_html(html, "https://example.com/p/1", ident)
    assert a.best_removal_link == "https://example.com/optout"
    assert a.has_opt_out_link
    assert "https://example.com/contact" in a.imprint_links or a.removal_links[-1]["url"].endswith("/contact")


def test_owner_from_copyright():
    assert owner_from_text("© 2024 Örnek Medya Yayıncılık A.Ş. Tüm hakları saklıdır") == "Örnek Medya Yayıncılık A.Ş"
    assert owner_from_text("Copyright 2020-2025 Example Directory Ltd. All rights reserved") == "Example Directory Ltd"
    assert owner_from_text("© 2024 Some Blog") == ""


def test_snippet_fallback(ident):
    a = analyze_snippet("Ahmet Yılmaz | LinkedIn", "İstanbul · Acme Yazılım", "https://linkedin.com/in/ahmetyilmaz34", ident)
    assert a.source == "snippet" and a.mentions == 0
    assert "Search snippet" in a.places
    assert DT_USERNAME in a.data_types and DT_CITY in a.data_types
    assert match_confidence(a) == "Likely"
