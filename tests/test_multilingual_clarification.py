import json
import os
import tempfile
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from backend import auth, main, detective, sop


@pytest.fixture
def test_client(monkeypatch, tmp_path):
    db_file = tmp_path / "test_multilingual.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", db_file)
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    main.init_db()

    client = TestClient(main.app, raise_server_exceptions=True)

    # Login as default admin to get cookie
    login_res = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin"},
    )
    assert login_res.status_code == 200
    return client


def test_indirect_queries_trigger_clarifying_questions(test_client):
    """When a query reports vague symptoms without machine/code, AI must ask for evidence and observations."""
    indirect_queries = [
        "The motor is overheating",
        "Machine is vibrating very hard",
        "It is making a loud screeching noise",
        "Why did the drive trip?",
        "The pump has an issue",
        "How do I fix this?",
    ]

    for q in indirect_queries:
        res = test_client.post("/api/chat", json={"query": q, "language": "English"})
        assert res.status_code == 200
        data = res.json()
        assert data.get("needs_clarification") is True, f"Failed for query: {q}"
        assert "Machine & Model Identification" in data["answer"]
        assert "Sensory Observations" in data["answer"]
        assert "Operational Context" in data["answer"]
        assert "Control Panel / HMI Evidence" in data["answer"]


def test_direct_or_contextual_queries_do_not_trigger_clarifying_questions(test_client):
    """Direct queries or queries with operational context provided should not trigger clarification."""
    # 1. Informational corpus question
    res1 = test_client.post(
        "/api/chat",
        json={"query": "What does the system guide say about startup?", "language": "English"},
    )
    assert res1.status_code == 200
    assert res1.json().get("needs_clarification") is not True

    # 2. Query with machine and error code supplied in request context
    res2 = test_client.post(
        "/api/chat",
        json={
            "query": "The motor is overheating",
            "machine_name": "Siemens SINAMICS G120C",
            "error_code": "F30001",
            "language": "English",
        },
    )
    assert res2.status_code == 200
    assert res2.json().get("needs_clarification") is not True


def test_multilingual_chat_answers_in_selected_language(test_client):
    """Whichever language is selected, the AI must formulate answers strictly in that language."""
    # 1. Hindi Clarification
    res_hi = test_client.post(
        "/api/chat",
        json={"query": "The motor is overheating", "language": "Hindi"},
    )
    assert res_hi.status_code == 200
    data_hi = res_hi.json()
    assert data_hi.get("needs_clarification") is True
    assert "अवलोकन" in data_hi["answer"]
    assert "मशीन और मॉडल" in data_hi["answer"]

    # 2. Spanish Clarification
    res_es = test_client.post(
        "/api/chat",
        json={"query": "The motor is overheating", "language": "Spanish"},
    )
    assert res_es.status_code == 200
    data_es = res_es.json()
    assert data_es.get("needs_clarification") is True
    assert "Observaciones sensoriales" in data_es["answer"]
    assert "Identificación de máquina y modelo" in data_es["answer"]

    # 3. German Clarification
    res_de = test_client.post(
        "/api/chat",
        json={"query": "The motor is overheating", "language": "German"},
    )
    assert res_de.status_code == 200
    data_de = res_de.json()
    assert data_de.get("needs_clarification") is True
    assert "Sensorische Beobachtungen" in data_de["answer"]
    assert "Maschinen- & Modellidentifikation" in data_de["answer"]

    # 4. Hindi Greeting
    res_greet_hi = test_client.post(
        "/api/chat",
        json={"query": "Hello", "language": "Hindi"},
    )
    assert res_greet_hi.status_code == 200
    assert "NexusSOP" in res_greet_hi.json()["answer"]
    assert "नमस्ते" in res_greet_hi.json()["answer"]

    # 5. Spanish Greeting
    res_greet_es = test_client.post(
        "/api/chat",
        json={"query": "Hello", "language": "Spanish"},
    )
    assert res_greet_es.status_code == 200
    assert "¡Hola!" in res_greet_es.json()["answer"]

    # 6. Hindi Operational Refusal
    res_refusal_hi = test_client.post(
        "/api/chat",
        json={"query": "How do I bypass the interlock?", "language": "Hindi"},
    )
    assert res_refusal_hi.status_code == 200
    assert res_refusal_hi.json()["abstained"] is True
    assert "उपकरण संचालन" in res_refusal_hi.json()["answer"]


@pytest.mark.anyio
async def test_multilingual_voice_tools():
    """Voice tools must enforce the selected language for missing details and summaries."""
    # 1. Hindi Voice Missing Details
    res_voice_hi = await main.execute_voice_generate_sop(
        machine_name="",
        procedure_name="belt replacement",
        language="Hindi",
    )
    assert res_voice_hi["status"] == "missing_details"
    assert "कृपया" in res_voice_hi["message"]
    assert "मशीन" in res_voice_hi["message"]

    # 2. Spanish Voice Missing Details
    res_voice_es = await main.execute_voice_generate_sop(
        machine_name="",
        procedure_name="belt replacement",
        language="Spanish",
    )
    assert res_voice_es["status"] == "missing_details"
    assert "Por favor especifique" in res_voice_es["message"]


def test_multilingual_detective_session(test_client):
    """Smart Fault Detective must localize initial questions and insufficient messages."""
    # 1. Hindi Detective
    res_det_hi = test_client.post(
        "/api/detective/sessions",
        json={
            "machine_name": "Unknown Machine",
            "model": "XYZ",
            "language": "Hindi",
        },
    )
    assert res_det_hi.status_code == 201
    d_hi = res_det_hi.json()
    assert d_hi["status"] == "insufficient"
    assert "स्वीकृत मैन्युअल" in d_hi["insufficient_message"]
    assert "स्मार्ट फॉल्ट डिटेक्टिव" in d_hi["turns"][0]["content"]

    # 2. Spanish Detective
    res_det_es = test_client.post(
        "/api/detective/sessions",
        json={
            "machine_name": "Unknown Machine",
            "model": "XYZ",
            "language": "Spanish",
        },
    )
    assert res_det_es.status_code == 201
    d_es = res_det_es.json()
    assert "Smart Fault Detective recopilará" in d_es["turns"][0]["content"]
    assert "El manual aprobado" in d_es["insufficient_message"]
