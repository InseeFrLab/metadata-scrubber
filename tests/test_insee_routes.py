"""Routes d'aide à la saisie de la source API (séries, opérations d'une série)."""

import os

import pytest

os.environ.setdefault("SCRUBBER_AUTH_PASSWORD", "testpw")

from fastapi.testclient import TestClient  # noqa: E402

import metadata_scrubber.insee_api as insee_api  # noqa: E402
import metadata_scrubber.ui.server as server  # noqa: E402
from api_fixtures import make_client  # noqa: E402

AUTH = {"Authorization": "Basic dTp0ZXN0cHc="}  # u:testpw


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(insee_api, "InseeMetadataClient", lambda: make_client()[0])
    monkeypatch.setattr(server, "_series_cache", {})
    monkeypatch.setattr(server, "_operations_cache", {})
    return TestClient(server.app, headers=AUTH)


def test_series_and_their_operations(client):
    assert client.get("/api/insee/series").json() == [{"id": "s1998", "label": "BTS"}]
    ops = client.get("/api/insee/series/s1998/operations").json()
    assert [o["id"] for o in ops] == ["s2022", "s2023", "s2024"]
    assert set(ops[0]) == {"id", "label"}


def test_unknown_serie_is_404(client):
    assert client.get("/api/insee/series/s0000/operations").status_code == 404
