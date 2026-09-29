from footprint_scanner.queries import Query, build_queries, dedupe, phone_formats, phone_key


def texts(qs, kind=None):
    return [q.text for q in qs if kind is None or q.kind == kind]


def test_main_query_first_and_unique(ident, sites):
    qs = build_queries(ident, sites)
    assert qs[0] == Query('"Ahmet Yılmaz"', "name_main")
    assert sum(q.is_main for q in qs) == 1


def test_turkish_and_ascii_forms_generated(ident, sites):
    names = texts(build_queries(ident, sites), "name")
    assert '"Ahmet Yilmaz"' in names
    assert '"Ahmet Kemal Yılmaz"' in names and '"Ahmet Kemal Yilmaz"' in names
    assert '"A. Yılmaz"' in names and '"A. Yilmaz"' in names
    # The main query isn't repeated as a plain name query.
    assert '"Ahmet Yılmaz"' not in names


def test_combined_queries(ident, sites):
    qs = build_queries(ident, sites)
    assert '"Ahmet Yilmaz" "İstanbul"' in texts(qs, "name_city")
    assert '"Ahmet Yılmaz" "Acme Yazılım"' in texts(qs, "name_employer")
    assert '"A. Yilmaz" "Boğaziçi Üniversitesi"' in texts(qs, "name_school")


def test_email_username_phone_queries(ident, sites):
    qs = build_queries(ident, sites)
    assert texts(qs, "email") == ['"ahmet.yilmaz@example.com"']
    assert texts(qs, "username") == ['"ahmetyilmaz34"']
    phones = texts(qs, "phone")
    for f in ['"+90 532 123 45 67"', '"05321234567"', '"532-123-4567"', '"+905321234567"', '"(532) 123 45 67"']:
        assert f in phones


def test_phone_formats_from_any_input_format():
    expected = phone_formats("+90 532 123 45 67")
    assert phone_formats("05321234567") == expected
    assert phone_formats("0090-532-123-4567") == expected
    assert phone_formats("532 123 4567") == expected
    assert len(expected) == len(set(expected)) == 7


def test_phone_key_normalizes():
    assert phone_key("+90 (532) 123 45 67") == phone_key("0532 123 4567") == "5321234567"


def test_non_turkish_phone_kept():
    assert phone_formats("+1 415 555 0100") == ["+1 415 555 0100", "+14155550100"]


def test_site_queries_grouped_and_per_domain(ident, sites):
    grouped = texts(build_queries(ident, sites), "site_broker")
    assert any("site:spokeo.com OR site:whitepages.com" in t for t in grouped)
    per = texts(build_queries(ident, sites, site_query_mode="per_domain"), "site_broker")
    assert '"Ahmet Yılmaz" site:spokeo.com' in per
    assert len(per) > len(grouped)
    assert any("site:linkedin.com" in t for t in texts(build_queries(ident, sites), "site_professional"))


def test_dedupe_is_case_insensitive_and_keeps_priority():
    qs = dedupe([Query('"ahmet yılmaz"', "name"), Query('"AHMET  YILMAZ"', "name_main"), Query('"x"', "email")])
    assert [q.kind for q in qs] == ["name_main", "email"]


def test_no_duplicates(ident, sites):
    qs = build_queries(ident, sites)
    assert len(texts(qs)) == len(set(texts(qs)))
