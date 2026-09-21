import os
import tempfile

import pytest

# Point the database at a throw-away file before importing the app.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["TIDE_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _payload():
    return {
        "samples": [
            {"time": f"2026-09-21T{h:02d}:00:00Z", "height_m": h_m}
            for h, h_m in [
                (0, 1.0), (1, 3.0), (2, 6.0), (3, 8.0),
                (4, 8.0), (5, 6.0), (6, 3.0), (7, 1.0),
            ]
        ],
        "query_start": "2026-09-21T00:00:00Z",
        "query_end": "2026-09-21T07:00:00Z",
        "draft_m": 4.0,
        "clearance_m": 1.0,
        "max_gap_seconds": 3700,
    }


def test_create_and_get_task(client):
    resp = client.post("/tasks", json=_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    task_id = body["task_id"]
    assert body["status"] in {"pending", "done"}
    assert body["request"]["draft_m"] == 4.0

    got = client.get(f"/tasks/{task_id}")
    assert got.status_code == 200
    result = got.json()["result"]
    assert got.json()["status"] == "done"
    assert result["threshold_m"] == 5.0
    assert result["unknown"] == []
    assert len(result["windows"]) == 1
    w = result["windows"][0]
    # Threshold crossings lie inside (01:00,02:00) and (05:00,06:00).
    assert w["start"].startswith("2026-09-21T01:")
    assert w["end"].startswith("2026-09-21T05:")
    assert w["min_height_m"] >= 5.0


def test_task_not_found(client):
    assert client.get("/tasks/doesnotexist").status_code == 404


def test_validation_rejects_non_increasing(client):
    payload = _payload()
    payload["samples"][3]["time"] = payload["samples"][2]["time"]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 422
    assert "递增" in resp.text


def test_gap_returns_unknown(client):
    payload = _payload()
    payload["samples"] = [
        {"time": "2026-09-21T00:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T01:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T05:00:00Z", "height_m": 8.0},
        {"time": "2026-09-21T06:00:00Z", "height_m": 8.0},
    ]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    result = client.get(f"/tasks/{resp.json()['task_id']}").json()["result"]
    assert len(result["windows"]) == 2
    reasons = {u["reason"] for u in result["unknown"]}
    assert "sample_gap_exceeds_max_gap" in reasons


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
