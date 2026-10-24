import asyncio
import base64
import io
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import main


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("POST", "https://example.invalid"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class FakeClient:
    """Records requests and replays queued responses for the LLM endpoint."""

    calls: list[dict] = []
    responses: list[FakeResponse] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        FakeClient.calls.append({"url": url, "headers": headers, "json": json})
        if FakeClient.responses:
            return FakeClient.responses.pop(0)
        return FakeResponse({
            "choices": [{"message": {"content": "Grounded answer [S1]."}}]
        })


@pytest.fixture
def fake_llm(monkeypatch):
    FakeClient.calls = []
    FakeClient.responses = []
    monkeypatch.setattr(main, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(main, "httpx", httpx)
    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    return FakeClient


def make_client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "test.sqlite3")
    return TestClient(main.app)


def test_seeded_policy_answer_quotes_approved_source(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with make_client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/chat", json={"query": "What does the lexical retrieval score mean?"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["grounded"] is True
    assert payload["generation_mode"] == "extractive"
    assert payload["citations"]
    assert payload["citations"][0]["reference_id"] == "S1"
    assert "retrieval score is a lexical relevance score" in payload["citations"][0]["passage"]


def test_operational_request_abstains(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        response = client.post("/api/chat", json={"query": "How do I repair a PLC fault?"})

    payload = response.json()
    assert payload["abstained"] is True
    assert payload["citations"] == []
    assert "approved site SOP" in payload["answer"]


@pytest.mark.parametrize(
    "query",
    [
        "How do I commission this process equipment?",
        "How do I configure the machine?",
        "How do I de-energize this line?",
        "How do I program the controller?",
    ],
)
def test_additional_work_requests_use_operational_refusal(query, tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        response = client.post("/api/chat", json={"query": query})

    payload = response.json()
    assert payload["abstained"] is True
    assert payload["reason"].startswith("Operational or safety-critical intent")
    assert payload["citations"] == []


def test_uploaded_document_is_not_retrievable_until_approved(tmp_path, monkeypatch):
    content = b"Escalation contacts are listed in the reviewed shift handbook for the site team."
    with make_client(tmp_path, monkeypatch) as client:
        upload = client.post(
            "/api/documents",
            files={"file": ("shift-handbook.txt", content, "text/plain")},
        )
        document_id = upload.json()["id"]
        pending_answer = client.post(
            "/api/chat", json={"query": "Where are escalation contacts listed?"}
        ).json()
        approval = client.post(f"/api/documents/{document_id}/approve")
        approved_answer = client.post(
            "/api/chat", json={"query": "Where are escalation contacts listed?"}
        ).json()

    assert upload.status_code == 201
    assert upload.json()["approved"] is False
    assert pending_answer["abstained"] is True
    assert approval.status_code == 200
    assert approved_answer["grounded"] is True
    assert approved_answer["citations"][0]["document_name"] == "shift-handbook.txt"


@pytest.mark.parametrize(
    "query",
    [
        "How do I repair a PLC fault?",
        "How do I commission this process equipment?",
        "How do I configure the machine?",
        "How do I de-energize this line?",
        "How do I program the controller?",
        "Lock out valve-402 before maintenance.",
        "Give me the startup procedure for the line.",
        "How do I bypass the interlock?",
        "How do I troubleshoot this compressor?",
        "How do I set up the extraction pump?",
        "Show me how to lubricate the gearbox.",
        "How do I isolate the line before service?",
        "Please walk me through restarting the compressor.",
        "What's the process for bypassing the interlock?",
        "I need the procedure for replacing the pump seal.",
        "Tell me how to isolate the line.",
        "How do I adjust the setpoint on the controller?",
        "What is the lockout procedure for valve-12?",
    ],
)
def test_work_requests_are_still_refused(query, tmp_path, monkeypatch):
    """Physical work on plant equipment must still be refused."""
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post("/api/chat", json={"query": query}).json()

    assert payload["abstained"] is True
    assert payload["citations"] == []
    assert payload["reason"].startswith("Operational or safety-critical intent")


@pytest.mark.parametrize(
    "query",
    [
        "What does the system guide say about startup?",
        "What are the steps for the escalation policy?",
        "How do I install a document in the vault?",
        "How do I configure the dashboard?",
        "How do I reset the retrieval threshold?",
        "How do I wire the knowledge vault section?",
        "What is the fault code policy?",
        "Which document describes valve-402?",
        "What does the guide say about maintenance schedules?",
        "How do I reset the retrieval score threshold?",
        "What is the procedure for document approval?",
        "Which source explains the escalation policy?",
        "How do I review the upload workflow?",
    ],
)
def test_informational_questions_are_not_refused(query, tmp_path, monkeypatch):
    """A keyword alone must not trigger the operational refusal.

    The question may still abstain for lack of evidence, but it must not be
    turned away as a request for equipment work.
    """
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post("/api/chat", json={"query": query}).json()

    reason = payload.get("reason") or ""
    assert not reason.startswith("Operational or safety-critical intent"), reason
    assert "equipment operating" not in payload["answer"]


def test_informational_question_with_matching_evidence_is_answered(tmp_path, monkeypatch):
    """A question the corpus does answer must be answered, not refused."""
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/chat", json={"query": "How do I install a document in the vault?"}
        ).json()

    assert payload["abstained"] is False
    assert payload["citations"]


def test_favicon_is_served(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


def test_root_serves_existing_frontend(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "NexusSOP | Industrial Evidence Assistant" in response.text


def test_escalation_is_persisted_in_analytics(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/escalations",
            json={"query": "How do I repair the machine?", "reason": "Operational request refused"},
        )
        analytics = client.get("/api/analytics").json()

    assert response.status_code == 201
    assert response.json()["status"] == "open"
    assert analytics["escalations"] == 1


def test_csv_export_neutralizes_formula_queries(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        client.post("/api/chat", json={"query": "=1+1"})
        response = client.get("/api/analytics/export")

    assert response.status_code == 200
    assert "'=1+1" in response.text


def test_live_route_serves_html(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        response = client.get("/live")

    assert response.status_code == 200
    assert "Live Audio Chat" in response.text


def test_voice_config_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "GEMINI_LIVE_MODEL", "gemini-3.8-live")
    with make_client(tmp_path, monkeypatch) as client:
        response = client.get("/api/voice/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["model"] == "gemini-3.8-live"
    assert payload["ws_url"] == "/api/voice/ws"
    assert "gemini_api_key" not in payload


def test_text_session_persists_messages(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        session = client.post("/api/sessions", json={"kind": "text"}).json()
        response = client.post(
            "/api/chat",
            json={"query": "What does the lexical retrieval score mean?", "session_id": session["session_id"]},
        )
        history = client.get(f"/api/sessions/{session['session_id']}").json()

    assert response.json()["session_id"] == session["session_id"]
    assert [message["role"] for message in history["messages"]] == ["user", "assistant"]
    assert history["messages"][0]["content"].startswith("What does")


def test_voice_session_endpoint_rejects_wrong_kind(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        session = client.post("/api/sessions", json={"kind": "voice"}).json()
        response = client.get(f"/api/sessions/{session['session_id']}")

    assert response.status_code == 200
    assert response.json()["session"]["kind"] == "voice"


def test_main_frontend_uses_secure_live_voice_relay():
    frontend = (main.ROOT / "industrial_genai_sop_assistant.html").read_text(encoding="utf-8")

    assert "gemini_api_key" not in frontend
    assert "mediaChunks" not in frontend
    assert "clientContent: { turnComplete: true }" not in frontend
    assert "const wsPath = cfg.ws_url || '/api/voice/ws';" in frontend
    assert "audio: { mimeType: 'audio/pcm;rate=16000', data: b64 }" in frontend
    assert "realtimeInput: { audioStreamEnd: true }" in frontend
    assert "functionDeclarations" in frontend
    assert "search_knowledge_base" in frontend
    assert "toolResponse: { functionResponses }" in frontend
    assert "/api/voice/knowledge" in frontend
    assert "MUST call search_knowledge_base" in frontend
    assert "result: toolResult" in frontend
    assert "nexusToolResult" in frontend


def test_voice_knowledge_returns_catalog_and_grounded_passages(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/voice/knowledge",
            json={"query": "What does the system guide say about the knowledge vault?"},
        ).json()

    assert payload["documents"]
    assert any(document["name"] == "system_guide.md" for document in payload["documents"])
    assert payload["citations"]
    assert "knowledge vault" in payload["citations"][0]["passage"].lower()
    assert payload["citations"][0]["source"]
    assert payload["documents"][0]["characters"] > 0


def test_voice_knowledge_lists_documents_for_catalog_questions(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/voice/knowledge",
            json={"query": "What documents are uploaded?"},
        ).json()

    assert payload["documents"]
    assert "Approved documents:" in payload["answer"]
    assert payload["abstained"] is False


@pytest.mark.parametrize("greeting", ["hi", "hello", "Hello there", "thanks", "thank you"])
def test_greeting_is_answered_instead_of_refused(greeting, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post("/api/chat", json={"query": greeting}).json()

    assert payload["abstained"] is False
    assert payload["grounded"] is True
    assert payload["answer"].strip()


def test_greeting_still_runs_retrieval_and_reaches_the_model(tmp_path, monkeypatch, fake_llm):
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post("/api/chat", json={"query": "hi"}).json()

    assert payload["abstained"] is False
    assert fake_llm.calls, "the LLM must be called for conversational questions"
    sent = fake_llm.calls[0]["json"]["messages"][-1]["content"]
    assert "hi" in sent


def test_llm_receives_retrieved_passages_and_citation_ids(tmp_path, monkeypatch, fake_llm):
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/chat", json={"query": "What does the lexical retrieval score mean?"}
        ).json()

    assert payload["grounded"] is True
    assert fake_llm.calls, "the LLM must be called for answerable questions"
    sent = fake_llm.calls[0]["json"]["messages"][-1]["content"]
    assert "[S1]" in sent
    assert "evidence_policy.md" in sent
    assert "retrieval score is a lexical relevance score" in sent


def test_answer_never_cites_a_source_that_was_not_retrieved(tmp_path, monkeypatch, fake_llm):
    fake_llm.responses.append(
        FakeResponse({"choices": [{"message": {"content": "Answer with a fake citation [S9]."}}]})
    )
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/chat", json={"query": "What does the lexical retrieval score mean?"}
        ).json()

    assert "[S9]" not in payload["answer"]
    assert "[S1]" in payload["answer"]


def test_answer_without_citation_is_completed_with_the_source_list(
    tmp_path, monkeypatch, fake_llm
):
    fake_llm.responses.append(
        FakeResponse({"choices": [{"message": {"content": "A plain answer with no citation."}}]})
    )
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/chat", json={"query": "What does the lexical retrieval score mean?"}
        ).json()

    assert payload["grounded"] is True
    assert "[S1]" in payload["answer"]


def test_transient_model_failure_is_retried(tmp_path, monkeypatch, fake_llm):
    fake_llm.responses.append(FakeResponse({}, status_code=503))
    fake_llm.responses.append(FakeResponse({}, status_code=503))
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/chat", json={"query": "What does the lexical retrieval score mean?"}
        ).json()

    assert len(fake_llm.calls) == 3
    assert payload["grounded"] is True
    assert payload["generation_mode"] == "grounded-llm"


def test_persistent_model_failure_falls_back_without_crashing(
    tmp_path, monkeypatch, fake_llm
):
    for _ in range(3):
        fake_llm.responses.append(FakeResponse({}, status_code=500))
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/chat", json={"query": "What does the lexical retrieval score mean?"}
        ).json()

    assert payload["grounded"] is True
    assert payload["generation_mode"] == "extractive-fallback"
    assert payload["citations"]


def test_irrelevant_question_still_abstains(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post("/api/chat", json={"query": "quantum flux capacitor"}).json()

    assert payload["abstained"] is True
    assert payload["citations"] == []


def test_word_forms_match_the_same_source(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/chat", json={"query": "How are approved documents reviewed?"}
        ).json()

    assert payload["grounded"] is True
    assert payload["citations"][0]["document_name"] == "document_review.md"


def test_headings_are_not_returned_as_bare_passages(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/chat", json={"query": "What happens to documents added by an operator?"}
        ).json()

    for citation in payload["citations"]:
        assert citation["passage"].strip() != "# Document review and escalation"
        assert len(citation["passage"]) > 60


# ---- Image upload to knowledge base tests ----

def _make_png_image() -> bytes:
    """Create a small PNG image containing text for OCR testing."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (400, 200), color="white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 24)
    except Exception:
        font = ImageFont.load_default()
    d.text((20, 50), "Escalation contacts are in room 402 ext 555", fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_image_upload_to_knowledge_base(tmp_path, monkeypatch):
    """Uploading an image document extracts text via OCR and indexes it."""
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    image_bytes = _make_png_image()
    with make_client(tmp_path, monkeypatch) as client:
        upload = client.post(
            "/api/documents",
            files={"file": ("safety-notice.png", image_bytes, "image/png")},
        )
    assert upload.status_code == 201
    payload = upload.json()
    assert payload["approved"] is False
    assert payload["image_path"] is not None
    assert payload["name"] == "safety-notice.png"

    # The OCR-extracted text should be indexed
    docs = client.get("/api/documents").json()["documents"]
    uploaded = next(d for d in docs if d["id"] == payload["id"])
    assert uploaded["image_path"] is not None


def test_image_document_queried_after_approval(tmp_path, monkeypatch):
    """An OCR'd image document can be retrieved via RAG once approved."""
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    image_bytes = _make_png_image()
    with make_client(tmp_path, monkeypatch) as client:
        upload = client.post(
            "/api/documents",
            files={"file": ("safety-notice.png", image_bytes, "image/png")},
        )
        assert upload.status_code == 201
        doc_id = upload.json()["id"]

        # Before approval: not retrievable
        pending = client.post(
            "/api/chat", json={"query": "Escalation contacts room 402 ext 555"}
        ).json()
        assert pending["abstained"] is True

        # Approve it
        approval = client.post(f"/api/documents/{doc_id}/approve")
        assert approval.status_code == 200

        # After approval: retrievable via RAG
        answered = client.post(
            "/api/chat", json={"query": "Escalation contacts room 402 ext 555"}
        ).json()
        assert answered["abstained"] is False
        assert answered["grounded"] is True
        assert len(answered["citations"]) > 0
        assert any(
            "safety-notice.png" in c["document_name"]
            for c in answered["citations"]
        )


def test_uploaded_image_is_served(tmp_path, monkeypatch):
    """Uploaded images are served via /api/uploads/{name}."""
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    image_bytes = _make_png_image()
    with make_client(tmp_path, monkeypatch) as client:
        upload = client.post(
            "/api/documents",
            files={"file": ("serve-me.png", image_bytes, "image/png")},
        )
        assert upload.status_code == 201
        image_path = upload.json()["image_path"]
        assert image_path is not None
        image_name = image_path.split("/")[-1]

        served = client.get(f"/api/uploads/{image_name}")
        assert served.status_code == 200
        assert served.headers["content-type"].startswith("image/")


def test_image_attachment_to_chat_query(tmp_path, monkeypatch, fake_llm):
    """An image attached to a chat query is passed to the model as image_url."""
    image_bytes = _make_png_image()
    data_url = "data:image/png;base64:" + base64.b64encode(image_bytes).decode()
    with make_client(tmp_path, monkeypatch) as client:
        payload = client.post(
            "/api/chat",
            json={"query": "What is in this image?", "image": data_url},
        ).json()

    assert payload["grounded"] is True
    assert fake_llm.calls, "the LLM must be called for multimodal queries"
    content = fake_llm.calls[0]["json"]["messages"][-1]["content"]
    # When an image is attached, content is a list of parts
    if isinstance(content, list):
        text_parts = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    else:
        text_parts = str(content)
    assert "[S1]" in text_parts
    assert "What is in this image?" in text_parts


def test_image_body_includes_filename_stem(tmp_path, monkeypatch):
    """Uploaded image document body includes the sanitized filename stem."""
    image_bytes = _make_png_image()
    with make_client(tmp_path, monkeypatch) as client:
        upload = client.post(
            "/api/documents",
            files={"file": ("my-safety-notice.png", image_bytes, "image/png")},
        )
    assert upload.status_code == 201
    doc_id = upload.json()["id"]
    conn = sqlite3.connect(main.DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT body FROM documents WHERE id = ?", (doc_id,)).fetchone()
    assert row is not None
    assert "[Image: my-safety-notice]" in row["body"]


def test_image_queried_by_filename(tmp_path, monkeypatch):
    """An image document can be retrieved by searching its filename."""
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    image_bytes = _make_png_image()
    with make_client(tmp_path, monkeypatch) as client:
        upload = client.post(
            "/api/documents",
            files={"file": ("safety-notice.png", image_bytes, "image/png")},
        )
        assert upload.status_code == 201
        doc_id = upload.json()["id"]
        client.post(f"/api/documents/{doc_id}/approve")

        # Query by filename stem
        payload = client.post(
            "/api/chat", json={"query": "safety-notice"}
        ).json()

    assert payload["abstained"] is False, payload.get("reason")
    assert payload["grounded"] is True
    assert any("safety-notice.png" in c["document_name"] for c in payload["citations"])
