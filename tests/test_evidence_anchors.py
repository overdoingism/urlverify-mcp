from urlverify_mcp.models import Evidence
from urlverify_mcp.rules import _anchors, _fragments, verify_quotes
from urlverify_mcp.checks.ct import parse_crtsh_cert_page

WIKI_RAW = """{
 "ok": true,
 "found": true,
 "title": "Electron (software framework)",
 "official_website": [
  "electronjs.org"
 ],
 "developer_fields": [
  "GitHub",
  "OpenJS Foundation"
 ],
 "stability": {"stable": true, "recent_change": false}
}"""


def test_double_quoted_values_and_ellipsis_quotes_anchor():
    ev = Evidence(kind="wikipedia", source="https://en.wikipedia.org/wiki/Electron_(software_framework)", tier=1,
                  claim="Electron is stewarded by the OpenJS Foundation",
                  quote='"developer_fields": ["GitHub", "OpenJS Foundation", ...]')
    assert "openjs foundation" in _anchors(ev) and "developer_fields" not in _anchors(ev)
    assert _fragments('"developer_fields": ["GitHub", "OpenJS Foundation", ...]')[0].startswith('"developer_fields"')
    verify_quotes([ev], {ev.source: WIKI_RAW})
    assert ev.verified_quote is True
    ev2 = Evidence(kind="wikipedia", source=ev.source, tier=1, claim="developer is Acme Corp", quote='"developer_fields": ["Acme Corp"]')
    verify_quotes([ev2], {ev.source: WIKI_RAW})
    assert ev2.verified_quote is False


def test_page_quote_with_ellipsis():
    ev = Evidence(kind="media", source="https://news.example/x", tier=2, claim="official domain is lmstudio.ai",
                  quote="LM Studio, built by Element Labs ... available at lmstudio.ai")
    verify_quotes([ev], {ev.source: "LM Studio, built by Element Labs, is available at lmstudio.ai for Mac"})
    assert ev.verified_quote is True


def test_crtsh_page_parsing():
    assert parse_crtsh_cert_page(200, "text/html; charset=UTF-8", "<HTML>... Certificate not found ...</HTML>") == {"ok": True, "logged": False}
    assert parse_crtsh_cert_page(200, "text/html", "<TD>crt.sh ID</TD><TD>123</TD> SHA-256 ...")["logged"] is True
    assert parse_crtsh_cert_page(200, "text/html", "<BR><BR>Unsupported output type: json</BODY></HTML>")["ok"] is False
    assert parse_crtsh_cert_page(503, "text/html", "")["error"] == "crt.sh HTTP 503"
