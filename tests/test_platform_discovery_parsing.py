from app.integrations.platform_discovery.public_sources import normalise_url, parse_html, validate_public_url

def test_parse_html_extracts_title_text_and_links():
    page, links = parse_html("https://example.com/docs",
        "<html><head><title>راهنما</title><script>secret()</script></head><body><h1>مستندات</h1><a href='/changelog'>تغییرات</a></body></html>", 200)
    assert page.title == "راهنما"
    assert "مستندات" in page.text
    assert "secret()" not in page.text
    assert links == ["/changelog"]

def test_normalise_url_removes_fragment():
    assert normalise_url("https://example.com/docs#intro") == "https://example.com/docs"

def test_reject_private_ip():
    try:
        validate_public_url("http://127.0.0.1")
    except ValueError:
        return
    raise AssertionError("private destinations must be rejected")
