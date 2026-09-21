import os
import tempfile
from datetime import datetime

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


def test_result_has_empty_departure_windows_without_new_fields(client):
    resp = client.post("/tasks", json=_payload())
    result = client.get(f"/tasks/{resp.json()['task_id']}").json()["result"]
    # Legacy behaviour is preserved: the field exists but is empty.
    assert result["departure_windows"] == []
    # Original outputs are unchanged.
    assert len(result["windows"]) == 1
    assert result["unknown"] == []


def test_transit_duration_returns_departure_windows(client):
    payload = _payload()
    payload["transit_duration_seconds"] = 3600
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    body = client.get(f"/tasks/{resp.json()['task_id']}").json()
    assert body["request"]["transit_duration_seconds"] == 3600.0
    result = body["result"]
    assert len(result["departure_windows"]) == 1
    dep = result["departure_windows"][0]
    win = result["windows"][0]
    # The whole occupancy must fit inside the safe window.
    assert dep["start"] >= win["start"]
    assert dep["duration_seconds"] > 0
    # Half-open end: end - start == duration_seconds.
    def parse(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))

    assert (
        parse(dep["end"]) - parse(dep["start"])
    ).total_seconds() == dep["duration_seconds"]
    # Last possible departure (one second before end) must still fit.
    assert parse(dep["end"]).timestamp() - 1 + 3600 <= parse(win["end"]).timestamp()


def test_maintenance_blocks_shifted_and_subtracted(client):
    payload = _payload()
    payload["transit_duration_seconds"] = 3600
    # Two abutting blocks merge into 04:30..05:30; the third extends past
    # query_end and is clipped to ..07:00 (it does not affect this window).
    payload["maintenance_blocks"] = [
        {"start": "2026-09-21T04:30:00Z", "end": "2026-09-21T05:00:00Z"},
        {"start": "2026-09-21T05:00:00Z", "end": "2026-09-21T05:30:00Z"},
        {"start": "2026-09-21T06:30:00Z", "end": "2026-09-21T09:00:00Z"},
    ]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    result = client.get(f"/tasks/{resp.json()['task_id']}").json()["result"]
    deps = result["departure_windows"]
    assert len(deps) == 1
    # Safe window 01:39:11..05:20:49 with D=3600; the merged closure
    # 04:30..05:30 shifted earlier by one hour forbids [03:30, 04:30).
    assert deps[0]["start"] == "2026-09-21T01:39:11Z"
    assert deps[0]["end"] == "2026-09-21T03:30:00Z"
    assert deps[0]["duration_seconds"] == 6649
    # Half-open semantics: 03:30:00 is the last viable second.
    assert deps[0]["end"] > deps[0]["start"]


def test_maintenance_without_duration_is_ignored(client):
    payload = _payload()
    payload["maintenance_blocks"] = [
        {"start": "2026-09-21T02:00:00Z", "end": "2026-09-21T03:00:00Z"}
    ]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    result = client.get(f"/tasks/{resp.json()['task_id']}").json()["result"]
    assert result["departure_windows"] == []


def test_validation_rejects_bad_maintenance_block(client):
    payload = _payload()
    payload["transit_duration_seconds"] = 3600
    payload["maintenance_blocks"] = [
        {"start": "2026-09-21T03:00:00Z", "end": "2026-09-21T02:00:00Z"}
    ]
    resp = client.post("/tasks", json=payload)
    assert resp.status_code == 422
    assert "maintenance_blocks" in resp.text


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
