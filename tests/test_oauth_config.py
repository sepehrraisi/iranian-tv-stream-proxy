"""OAuth configuration supports a single-header shortcut and split values."""

import server


def test_complete_authorization_header_takes_precedence(monkeypatch):
    header = (
        'OAuth oauth_consumer_key="authorized", oauth_nonce="fixed", '
        'oauth_signature="signature", oauth_signature_method="HMAC-SHA1", '
        'oauth_timestamp="1", oauth_token="token", oauth_version="1.0"'
    )
    monkeypatch.setattr(server, "SEPEHR_OAUTH_AUTHORIZATION", header)
    monkeypatch.setattr(server, "SEPEHR_OAUTH_CONSUMER_KEY", "ignored")
    monkeypatch.setattr(server, "SEPEHR_OAUTH_SIGNATURE", "ignored")
    monkeypatch.setattr(server, "SEPEHR_OAUTH_TOKEN", "ignored")

    assert server._oauth_authorization() == header


def test_missing_oauth_configuration_uses_fallback(monkeypatch):
    monkeypatch.setattr(server, "SEPEHR_OAUTH_AUTHORIZATION", "")
    monkeypatch.setattr(server, "SEPEHR_OAUTH_CONSUMER_KEY", "")
    monkeypatch.setattr(server, "SEPEHR_OAUTH_SIGNATURE", "")
    monkeypatch.setattr(server, "SEPEHR_OAUTH_TOKEN", "")

    assert server._oauth_authorization() is None
