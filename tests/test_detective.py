import pytest
from fastapi.testclient import TestClient

from backend import detective, main


G120C_MANUAL = """
# Siemens SINAMICS G120C fault F30001

[[page:142]]
## Fault F30001 overcurrent

Fault description: F30001 indicates an overcurrent condition on the SINAMICS G120C power module.

Possible causes:
- Motor cable short circuit displayed on the operator panel
- Drive operating in regenerative mode during rapid deceleration
- Cooling fan not running, shown as a warning on the BOP display

Recommended checks:
- Check whether the operator panel displays F30001 overcurrent
- Verify that the BOP display shows a cooling-fan warning
- Observe whether the fault occurred during rapid deceleration
"""

UNSAFE_ONLY_MANUAL = """
# Contactor Panel CP-9

[[page:8]]
## Fault E99

Possible causes: defective main contactor.

Recommended checks:
- Measure the incoming voltage with a multimeter
- Open the cabinet and replace the main contactor
- Disconnect live wires and probe the current
"""

IRRELEVANT_MANUAL = """
# Lathe lubrication chart

[[page:2]]
Fill the oil reservoir to the sight glass with ISO VG 68 spindle oil.
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "detective.sqlite3")
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with TestClient(main.app) as test_client:
        login = test_client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert login.status_code == 200
        yield test_client


def _upload_and_approve(client: TestClient, name: str, body: str, approve: bool = True) -> int:
    upload = client.post(
        "/api/documents",
        files={"file": (name, body.encode("utf-8"), "text/markdown")},
    )
    assert upload.status_code == 201
    doc_id = upload.json()["id"]
    if approve:
        approved = client.post(f"/api/documents/{doc_id}/approve")
        assert approved.status_code == 200
    return doc_id


def test_detective_uses_only_approved_manual_and_cites_page(client):
    pending_id = _upload_and_approve(
        client,
        "pending_g120c.md",
        G120C_MANUAL.replace("F30001", "FAKE99"),
        approve=False,
    )
    approved_id = _upload_and_approve(client, "sinamics_g120c.md", G120C_MANUAL)

    start = client.post(
        "/api/detective/sessions",
        json={
            "machine_name": "Siemens SINAMICS",
            "machine_model": "G120C",
            "error_code": "F30001",
            "initial_symptom": "Drive tripped",
        },
    )
    assert start.status_code == 201
    payload = start.json()
    assert payload["status"] == "collecting"
    assert payload["current_question"]["kind"] == "general"
    assert payload["current_question"]["content"] == "What happened?"
    assert all(m["id"] != pending_id for m in payload["approved_manuals"])
    assert any(m["id"] == approved_id for m in payload["approved_manuals"])

    session_id = payload["id"]
    for answer in ("Drive tripped on start", "This shift", "Red alarm on BOP"):
        payload = client.post(
            f"/api/detective/sessions/{session_id}/answer",
            json={"answer": answer},
        ).json()

    assert payload["status"] == "questioning"
    question = payload["current_question"]
    assert question["kind"] == "technical"
    source = question["source"]
    assert source["document_id"] == approved_id
    assert source["page"] == 142
    assert "F30001" in source["section"] or "overcurrent" in source["excerpt"].lower()
    assert "FAKE99" not in question["content"]
    assert "multimeter" not in question["content"].lower()
    assert "open the cabinet" not in question["content"].lower()


def test_detective_insufficient_when_manual_has_no_fault_info(client):
    _upload_and_approve(client, "lathe_oil.md", IRRELEVANT_MANUAL)
    start = client.post(
        "/api/detective/sessions",
        json={
            "machine_name": "Lathe",
            "machine_model": "LT-104",
            "error_code": "F30001",
        },
    ).json()
    session_id = start["id"]
    payload = start
    for answer in ("Unknown trip", "Yesterday", "No display message"):
        payload = client.post(
            f"/api/detective/sessions/{session_id}/answer",
            json={"answer": answer},
        ).json()
    assert payload["current_question"]["kind"] == "insufficient"
    assert payload["current_question"]["content"] == detective.INSUFFICIENT_MESSAGE


def test_detective_does_not_ask_unsafe_inspections(client):
    _upload_and_approve(client, "contactor_cp9.md", UNSAFE_ONLY_MANUAL)
    start = client.post(
        "/api/detective/sessions",
        json={"machine_name": "Contactor Panel", "machine_model": "CP-9", "error_code": "E99"},
    ).json()
    session_id = start["id"]
    payload = start
    for answer in ("Panel faulted", "An hour ago", "E99 on the lamp"):
        payload = client.post(
            f"/api/detective/sessions/{session_id}/answer",
            json={"answer": answer},
        ).json()
    text = payload["current_question"]["content"].lower()
    assert "multimeter" not in text
    assert "disconnect" not in text
    assert payload["current_question"]["kind"] in {"safety_escalation", "insufficient"}


def test_detective_requires_machine_and_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "detective_auth.sqlite3")
    with TestClient(main.app) as anon:
        denied = anon.post(
            "/api/detective/sessions",
            json={"machine_name": "Drive"},
        )
        assert denied.status_code == 401
    with TestClient(main.app) as authed:
        authed.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        missing = authed.post("/api/detective/sessions", json={"machine_name": ""})
        assert missing.status_code == 422
