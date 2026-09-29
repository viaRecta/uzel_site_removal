from footprint_scanner.textnorm import ascii_translit, count_mentions, fold, spelling_forms
from footprint_scanner.urls import normalize_url, registered_domain, url_key


def test_fold_turkish_forms_are_equal():
    assert fold("Ahmet Yılmaz") == fold("AHMET YILMAZ") == fold("ahmet yilmaz") == "ahmet yilmaz"
    assert fold("İSTANBUL") == fold("istanbul") == fold("Istanbul")
    assert fold("ŞÇĞÜÖ") == "scguo"


def test_ascii_translit_keeps_case():
    assert ascii_translit("Ahmet Yılmaz") == "Ahmet Yilmaz"
    assert ascii_translit("İstanbul Şişli Çağlayan Göğüş Ünlü") == "Istanbul Sisli Caglayan Gogus Unlu"


def test_spelling_forms_dedupes_ascii_names():
    assert spelling_forms("Ahmet Yılmaz") == ["Ahmet Yılmaz", "Ahmet Yilmaz"]
    assert spelling_forms("John Smith") == ["John Smith"]


def test_count_mentions_case_and_turkish_insensitive():
    text = "AHMET YILMAZ said hi. Ahmet Yilmaz again, and ahmet yılmaz. Not Mehmet Yılmaz or Ahmetyilmaz."
    assert count_mentions(text, ["Ahmet Yılmaz"]) == 3


def test_count_mentions_overlapping_variants_count_once():
    text = "Ahmet Kemal Yılmaz (A. Yılmaz) - Ahmet Yılmaz"
    names = ["Ahmet Yılmaz", "Ahmet Kemal Yılmaz", "A. Yılmaz", "Yılmaz"]
    # 'Yılmaz' overlaps each of the three mentions, so it must not add extra counts.
    assert count_mentions(text, names) == 3


def test_count_mentions_hyphenated_slug_style():
    assert count_mentions("profile/ahmet-yilmaz-istanbul", ["Ahmet Yılmaz"]) == 1


def test_normalize_strips_tracking_fragment_trailing_slash():
    url = "HTTPS://WWW.Example.com:443/Profile/ahmet/?utm_source=x&b=2&fbclid=abc&a=1#section"
    assert normalize_url(url) == "https://www.example.com/Profile/ahmet?a=1&b=2"


def test_normalize_root_and_port():
    assert normalize_url("http://example.com/") == "http://example.com"
    assert normalize_url("http://example.com:8080/x/") == "http://example.com:8080/x"


def test_url_key_ignores_scheme_and_www():
    assert url_key("http://www.example.com/a/?gclid=1") == url_key("https://example.com/a") == "example.com/a"


def test_registered_domain():
    assert registered_domain("https://tr.linkedin.com/in/x") == "linkedin.com"
    assert registered_domain("https://www.hurriyet.com.tr/haber") == "hurriyet.com.tr"
    assert registered_domain("foo.blogspot.com") == "foo.blogspot.com"  # private suffix


def test_user_agent_made_ascii():
    from footprint_scanner.config import DEFAULT_USER_AGENT, ascii_user_agent

    assert ascii_user_agent("Önder Uzel") == "Onder Uzel"
    assert ascii_user_agent("footprint-scanner/0.1 (Şişli; privacy@örnek.com)") == "footprint-scanner/0.1 (Sisli; privacy@ornek.com)"
    assert ascii_user_agent("  ") == DEFAULT_USER_AGENT
