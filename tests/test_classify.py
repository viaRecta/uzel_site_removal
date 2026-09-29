import httpx
import respx

from footprint_scanner.analyze import PageAnalysis
from footprint_scanner.classify import (
    FALLBACK_GOOGLE,
    classify_category,
    content_type,
    deindex_fallback,
    jurisdiction_text,
    matches_keep_list,
    own_account,
    removal_method,
    severity,
)
from footprint_scanner.enrich import Enricher, parse_rdap_domain


def test_categories(sites):
    assert classify_category("https://www.spokeo.com/Ahmet-Yilmaz", sites) == "Data broker / people search"
    assert classify_category("https://tr.linkedin.com/in/x", sites) == "Professional network"
    assert classify_category("https://www.hurriyet.com.tr/gundem/x", sites) == "News / media"
    assert classify_category("https://x.blogspot.com/2020/post", sites) == "Blog"
    assert classify_category("https://www.ankara.bel.tr/duyuru", sites) == "Government / court record"
    assert classify_category("https://web.archive.org/web/2020/x", sites) == "Archive / cache"
    assert classify_category("https://haberportal.example/x", sites) == "News / media"
    assert classify_category("https://unknown.example/page", sites) == "Other"


def test_category_names_exist_in_lists(sites):
    lists = {
        "Data broker / people search", "News / media", "Social media", "Forum / community", "Blog",
        "Business directory", "Government / court record", "Professional network", "Image / video host",
        "Archive / cache", "Review site", "Other",
    }
    assert sites.all_category_names() <= lists


def test_own_account():
    u = ["ahmetyilmaz34"]
    assert own_account("https://www.instagram.com/ahmetyilmaz34/", "Social media", u) == "Yes"
    assert own_account("https://www.tiktok.com/@AhmetYilmaz34", "Social media", u) == "Yes"
    assert own_account("https://ahmetyilmaz34.blogspot.com/post", "Blog", u) == "Yes"
    assert own_account("https://www.instagram.com/someoneelse/", "Social media", u) == "Unknown"
    assert own_account("https://news.example/ahmetyilmaz34", "News / media", u) == "No"


def test_keep_list():
    assert matches_keep_list("https://www.linkedin.com/in/ahmet-yilmaz", ["linkedin.com/in/ahmet-yilmaz"])
    assert not matches_keep_list("https://www.linkedin.com/in/other", ["linkedin.com/in/ahmet-yilmaz"])


def test_content_type():
    assert content_type("https://a.com/cv.pdf", "Other", None) == "PDF / document"
    assert content_type("https://youtube.com/watch?v=1", "Image / video host", None) == "Video"
    assert content_type("https://spokeo.com/x", "Data broker / people search", None) == "Listing"
    assert content_type("https://x.com/u", "Social media", None) == "Profile page"
    assert content_type("https://blog.com/p", "Blog", PageAnalysis(places=["Comments section"])) == "Comment"
    assert content_type("https://blog.com/p", "Blog", PageAnalysis(places=["Body"])) == "Text"


def test_severity():
    assert severity(["Full name", "TC Kimlik No"], "Other") == "Critical"
    assert severity(["Full name", "IBAN / financial"], "Other") == "Critical"
    assert severity(["Full name", "Address", "Photo"], "Other") == "Critical"
    assert severity(["Full name", "Phone"], "Other") == "High"
    assert severity(["Full name"], "Data broker / people search") == "High"
    assert severity(["Full name", "Employer"], "Other") == "Medium"
    assert severity(["Full name"], "Social media") == "Medium"
    assert severity(["Full name"], "News / media") == "Low"


def test_removal_method():
    assert removal_method("Social media", "Yes", False, [], "instagram.com") == "Client deletes own account"
    assert removal_method("Data broker / people search", "No", True, ["US"], "spokeo.com") == "Site opt-out form"
    assert removal_method("Data broker / people search", "No", False, ["US"], "x.com") == "Direct request to site owner"
    assert removal_method("News / media", "No", False, [], "hurriyet.com.tr") == "KVKK request (Law 6698)"
    assert removal_method("News / media", "No", False, ["TR"], "example.com") == "KVKK request (Law 6698)"
    assert removal_method("Blog", "No", False, ["DE"], "example.com") == "GDPR Art. 17 erasure request"
    assert removal_method("Blog", "No", False, [], "example.fr") == "GDPR Art. 17 erasure request"
    assert removal_method("Blog", "No", False, ["US"], "example.com") == "Direct request to site owner"


def test_deindex_fallback():
    assert deindex_fallback(["Full name", "Phone"]) == FALLBACK_GOOGLE
    assert deindex_fallback(["Full name", "Employer"]) == ""


def test_jurisdiction_text():
    assert jurisdiction_text(registrant="TR", hosting="TR") == "Turkey"
    assert jurisdiction_text(registrant="TR", hosting="DE") == "Turkey (registrant); Germany (hosting)"
    assert jurisdiction_text(operator="US") == "USA (operator)"
    assert jurisdiction_text() == ""


def test_parse_rdap_domain_redacted_and_real():
    real = {
        "entities": [
            {
                "roles": ["registrant"],
                "vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["org", {}, "text", "Example Media Ltd"],
                                          ["adr", {"cc": "GB"}, "text", ["", "", "", "", "", "", ""]]]],
            }
        ]
    }
    assert parse_rdap_domain(real) == ("Example Media Ltd", "GB")
    redacted = {
        "entities": [{"roles": ["registrant"], "vcardArray": ["vcard", [["org", {}, "text", "REDACTED FOR PRIVACY"],
                                                                         ["adr", {}, "text", ["", "", "", "", "", "", "Turkey"]]]]}]
    }
    assert parse_rdap_domain(redacted) == ("", "TR")


@respx.mock
async def test_wayback_lookup():
    respx.get("https://archive.org/wayback/available").mock(
        side_effect=[
            httpx.Response(200, json={"archived_snapshots": {"closest": {"available": True, "url": "x"}}}),
            httpx.Response(200, json={"archived_snapshots": {}}),
            httpx.Response(503),
        ]
    )
    async with httpx.AsyncClient() as c:
        e = Enricher(c)
        assert await e.archived("https://a.com") == "Yes"
        assert await e.archived("https://b.com") == "No"
        assert await e.archived("https://c.com") == "Unknown"
        assert await Enricher(c, wayback=False).archived("https://a.com") == "Unknown"
