"""Tests for the local upstream OAuth importer."""

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "import_upstream_oauth.py"
SPEC = importlib.util.spec_from_file_location("import_upstream_oauth", SCRIPT)
IMPORTER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(IMPORTER)


def test_extracts_three_upstream_values():
    source = '''
    return f'OAuth oauth_consumer_key="consumer", oauth_nonce="{nonce}",
    oauth_signature="signature", oauth_signature_method="HMAC-SHA1",
    oauth_timestamp="{timestamp}", oauth_token="token", oauth_version="1.0"'
    '''

    assert IMPORTER.extract_oauth_values(source) == {
        "SEPEHR_OAUTH_CONSUMER_KEY": "consumer",
        "SEPEHR_OAUTH_SIGNATURE": "signature",
        "SEPEHR_OAUTH_TOKEN": "token",
    }


def test_updates_existing_env_without_printing_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "STREAM_PORT=8765\nSEPEHR_OAUTH_TOKEN=old\n", encoding="utf-8"
    )
    values = {
        "SEPEHR_OAUTH_CONSUMER_KEY": "consumer",
        "SEPEHR_OAUTH_SIGNATURE": "signature",
        "SEPEHR_OAUTH_TOKEN": "new-token",
    }

    IMPORTER.update_env(env_file, values)

    assert env_file.read_text(encoding="utf-8") == (
        "STREAM_PORT=8765\n"
        "SEPEHR_OAUTH_TOKEN=new-token\n\n"
        "SEPEHR_OAUTH_CONSUMER_KEY=consumer\n"
        "SEPEHR_OAUTH_SIGNATURE=signature\n"
    )
