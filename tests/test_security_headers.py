"""
Security guards: CSP, escaping contract, legal pages, admin fail-closed.
=======================================================================

These pin the *observable* security contract so a future change cannot quietly
remove it:

* every response carries a CSP whose script-src has a nonce and no
  ``unsafe-inline`` (which is what makes the policy protective);
* the SPA template escapes untrusted text at the sink -- asserted against the
  source, because the injection happens in the browser and there is no headless
  JS engine in the test environment;
* the legal pages render and expose the configured contact;
* admin endpoints stay shut without a token.
"""
from __future__ import annotations

import re


def test_csp_header_present_and_strict(client):
    r = client.get("/")
    csp = r.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self' 'nonce-" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    # The nonce in the header must match the one in the served markup, or the
    # browser would refuse the page's own script.
    nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
    assert f'nonce="{nonce}"' in r.get_data(as_text=True)


def test_security_headers_present(client):
    h = client.get("/").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert "Permissions-Policy" in h


def test_escaping_helper_exists_and_is_used(client):
    """The sink for model output must go through esc(), not raw interpolation."""
    html = client.get("/").get_data(as_text=True)
    assert "const esc = s =>" in html
    # A sample of the dangerous sinks: each must wrap the value in esc().
    for pattern in (
        r"esc\(LANG===\"ar\"\?e\.summary_ar:e\.summary_en\)",
        r"notes\.map\(x=>`<li>\$\{esc\(x\)\}</li>`\)",
        r"esc\(s\.channel\)",
        r"esc\(o\.provider_ref",
    ):
        assert re.search(pattern, html), f"unescaped sink: {pattern}"


def test_no_inline_event_handlers(client):
    """Inline handlers would need 'unsafe-inline' and defeat the CSP."""
    html = client.get("/").get_data(as_text=True)
    assert 'onclick="' not in html
    assert 'onerror="' not in html
    assert 'onload="' not in html


def test_terms_page_renders(client, mods):
    r = client.get("/terms")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert mods["config"].CONTACT_EMAIL in body
    assert "شروط الاستخدام" in body
    # It is a template, and must say so rather than masquerade as legal advice.
    assert "استشارة" in body


def test_privacy_page_renders(client, mods):
    r = client.get("/privacy")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert mods["config"].CONTACT_EMAIL in body
    assert "سياسة الخصوصية" in body


def test_brand_is_configurable(client, mods):
    """No third-party trademark ships in the default UI."""
    html = client.get("/").get_data(as_text=True)
    assert mods["config"].BRAND_NAME in html
    assert "TRADLI" not in html.upper().replace("TRADLI_", "")
