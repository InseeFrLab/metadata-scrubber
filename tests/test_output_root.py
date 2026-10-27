"""Racine des sorties par défaut fournie par le serveur (instance V2 à part)."""

import os

os.environ.setdefault("SCRUBBER_AUTH_PASSWORD", "testpw")

from fastapi.testclient import TestClient  # noqa: E402

import metadata_scrubber.ui.server as server  # noqa: E402

AUTH = {"Authorization": "Basic dTp0ZXN0cHc="}  # u:testpw


def test_page_carries_output_root(monkeypatch):
    monkeypatch.setattr(server, "DEFAULT_OUTPUT_ROOT", 's3://b/out_v2"<x>')
    html = TestClient(server.app).get("/", headers=AUTH).text
    assert '<meta name="scrubber-output-root" content="s3://b/out_v2&quot;&lt;x&gt;">' in html
    assert "{{OUTPUT_ROOT}}" not in html
