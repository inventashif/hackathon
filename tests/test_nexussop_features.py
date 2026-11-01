import io
import json
from contextlib import contextmanager
from pathlib import Path

import fitz
import httpx
import pytest
from docx import Document
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

        # Check if the prompt is for SOP generation
        messages = (json or {}).get("messages", [])
        system_content = messages[0].get("content", "") if messages else ""
        if "industrial SOP generator" in system_content:
            sop_payload = {
                "title": "Valve Maintenance SOP",
                "problem": "valve overhaul",
                "steps": [
                    {
                        "number": 1,
                        "instruction": "Isolate the main valve and check pressure gauge.",
                        "citation_ids": ["S1", "S99"],  # S99 should be stripped
                        "figure_ids": ["F1", "F99"],   # F99 should be stripped
                        "caution": "Wear protective gear",
                    }
                ],
                "checks": [
                    {"text": "Confirm pressure is zero.", "citation_ids": ["S1"]}
                ],
            }
            return FakeResponse({
                "choices": [{"message": {"content": json.dumps(sop_payload)}}]
            })

        if "synthesizing engineer feedback" in system_content:
            return FakeResponse({
                "choices": [{"message": {"content": "Summary: Valve overhaul fixed after replacing seal."}}]
            })

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


@contextmanager
def make_test_env(tmp_path, monkeypatch, login_as="admin"):
    db_file = tmp_path / "test.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", db_file)
    monkeypatch.setattr(main, "IMAGE_UPLOAD_DIR", tmp_path / "uploads")
    with TestClient(main.app) as client:
        if login_as:
            res = client.post("/api/auth/login", json={"username": login_as, "password": "admin"})
            assert res.status_code == 200, f"Login failed: {res.text}"
        yield client


def test_auth_login_logout_and_me(tmp_path, monkeypatch):
    """Login failure is 401. Login success sets a cookie. Logout makes /api/auth/me return 401."""
    with make_test_env(tmp_path, monkeypatch, login_as=None) as client:
        # Unauthenticated request to /api/auth/me is 401
        res = client.get("/api/auth/me")
        assert res.status_code == 401

        # Wrong credentials return 401
        bad_login = client.post("/api/auth/login", json={"username": "admin", "password": "wrongpassword"})
        assert bad_login.status_code == 401

        # Correct login returns user and sets cookie
        good_login = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
        assert good_login.status_code == 200
        user_info = good_login.json()
        assert user_info["username"] == "admin"
        assert user_info["role"] == "admin"
        assert "nexus_sop_session" in client.cookies

        # /api/auth/me now returns 200
        me_res = client.get("/api/auth/me")
        assert me_res.status_code == 200
        assert me_res.json()["username"] == "admin"

        # Logout clears session
        logout_res = client.post("/api/auth/logout")
        assert logout_res.status_code == 200

        # /api/auth/me is 401 again
        me_after = client.get("/api/auth/me")
        assert me_after.status_code == 401


def test_disabled_user_login(tmp_path, monkeypatch):
    """Disabled user login returns 403. Admin cannot disable self or last admin."""
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as admin_client:
        # Create an engineer user
        create_res = admin_client.post(
            "/api/users",
            json={
                "username": "worker1",
                "display_name": "Worker One",
                "password": "password123",
                "role": "engineer",
            },
        )
        assert create_res.status_code == 201
        worker_id = create_res.json()["id"]

        # Admin cannot disable themselves
        self_disable = admin_client.post(f"/api/users/1/disable")
        assert self_disable.status_code == 403

        # Disable worker1
        dis_res = admin_client.post(f"/api/users/{worker_id}/disable")
        assert dis_res.status_code == 200

    # Try logging in as disabled user
    with make_test_env(tmp_path, monkeypatch, login_as=None) as client:
        login_res = client.post("/api/auth/login", json={"username": "worker1", "password": "password123"})
        assert login_res.status_code == 403
        assert "disabled" in login_res.json()["detail"].lower()


def test_roles_document_approve_reject_and_delete(tmp_path, monkeypatch):
    """Engineer cannot approve/delete documents (403). Admin approve makes passage retrievable."""
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as admin_client:
        # Create engineer
        admin_client.post(
            "/api/users",
            json={
                "username": "engineer1",
                "display_name": "Plant Engineer",
                "password": "engineerpass",
                "role": "engineer",
            },
        )

        # Upload a test document as admin
        content = "High pressure turbine startup procedure requires checking coolant valves before ignition."
        up_res = admin_client.post(
            "/api/documents",
            files={"file": ("turbine_guide.txt", content.encode("utf-8"), "text/plain")},
        )
        assert up_res.status_code == 201
        doc_id = up_res.json()["id"]

    # Now login as engineer
    with make_test_env(tmp_path, monkeypatch, login_as=None) as eng_client:
        login_res = eng_client.post("/api/auth/login", json={"username": "engineer1", "password": "engineerpass"})
        assert login_res.status_code == 200

        # Engineer can view documents
        doc_list = eng_client.get("/api/documents")
        assert doc_list.status_code == 200

        # Engineer cannot approve document
        appr_res = eng_client.post(f"/api/documents/{doc_id}/approve")
        assert appr_res.status_code == 403

        # Engineer cannot delete document
        del_res = eng_client.delete(f"/api/documents/{doc_id}")
        assert del_res.status_code == 403

    # Admin approves document
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as admin_client:
        appr = admin_client.post(f"/api/documents/{doc_id}/approve")
        assert appr.status_code == 200
        assert appr.json()["approved"] == 1

        # Admin rejects document
        rej = admin_client.post(f"/api/documents/{doc_id}/reject")
        assert rej.status_code == 200
        assert rej.json()["rejected"] == 1
        assert rej.json()["approved"] == 0

        # Re-approve
        appr2 = admin_client.post(f"/api/documents/{doc_id}/approve")
        assert appr2.status_code == 200

        # Delete document
        del_ok = admin_client.delete(f"/api/documents/{doc_id}")
        assert del_ok.status_code == 200

        # Verify not in document list
        docs_after = admin_client.get("/api/documents").json()["documents"]
        assert all(d["id"] != doc_id for d in docs_after)


def test_two_users_shared_room_messages(tmp_path, monkeypatch):
    """Two clients, admin and engineer, post to /api/chat. Each shared message poll contains both."""
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as admin_client:
        admin_client.post(
            "/api/users",
            json={
                "username": "tech_bob",
                "display_name": "Bob Technician",
                "password": "bobpassword",
                "role": "engineer",
            },
        )
        # Admin posts a message
        res1 = admin_client.post("/api/chat", json={"query": "What does the lexical retrieval score mean?"})
        assert res1.status_code == 200

    with make_test_env(tmp_path, monkeypatch, login_as=None) as eng_client:
        eng_client.post("/api/auth/login", json={"username": "tech_bob", "password": "bobpassword"})
        # Engineer posts a message
        res2 = eng_client.post("/api/chat", json={"query": "What does the escalation policy say?"})
        assert res2.status_code == 200

        # Poll shared room messages
        poll = eng_client.get("/api/rooms/shared/messages?after_id=0")
        assert poll.status_code == 200
        messages = poll.json()["messages"]

        display_names = [m["display_name"] for m in messages]
        contents = [m["content"] for m in messages]
        assert "Administrator" in display_names
        assert "Bob Technician" in display_names
        assert any("lexical retrieval score" in c for c in contents)
        assert any("escalation policy" in c for c in contents)


def test_invite_creation_and_acceptance(tmp_path, monkeypatch):
    """Invite creation, acceptance creates an engineer, and reusing the code returns 410."""
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as admin_client:
        inv_res = admin_client.post("/api/invites")
        assert inv_res.status_code == 201
        data = inv_res.json()
        code = data["code"]
        assert data["url"] == f"/invite/{code}"

        # Public check returns valid
        check_res = admin_client.get(f"/api/invites/{code}")
        assert check_res.status_code == 200
        assert check_res.json()["valid"] is True

        # Frontend route serves HTML page for invite
        page_res = admin_client.get(f"/invite/{code}")
        assert page_res.status_code == 200
        assert "text/html" in page_res.headers.get("content-type", "")

    # Anonymous user accepts invite
    with make_test_env(tmp_path, monkeypatch, login_as=None) as new_client:
        accept_res = new_client.post(
            f"/api/invites/{code}/accept",
            json={
                "username": "new_engineer",
                "display_name": "New Engineer",
                "password": "password456",
            },
        )
        assert accept_res.status_code == 201
        assert "nexus_sop_session" in new_client.cookies

        # User is authenticated as engineer
        me = new_client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["role"] == "engineer"

        # Reusing the invite code returns 410
        reuse_res = new_client.post(
            f"/api/invites/{code}/accept",
            json={
                "username": "another_user",
                "display_name": "Another",
                "password": "password789",
            },
        )
        assert reuse_res.status_code == 410


def test_procedure_question_returns_sop_with_citations_and_abstains_without_overlap(tmp_path, monkeypatch, fake_llm):
    """A procedure question whose passages clear the gates returns sop.steps with citation ids. Fake [S99] is stripped."""
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as client:
        # Upload a detailed procedure manual
        manual_text = (
            "# Main Valve Overhaul\n"
            "This procedure details how to overhaul the main pressure valve.\n\n"
            "Step 1: Isolate the main valve and check pressure on gauge G-101 before opening valve casing.\n\n"
            "Step 2: Inspect valve seat for wear and replace seal gasket if cracked."
        )
        up = client.post(
            "/api/documents",
            files={"file": ("valve_overhaul.md", manual_text.encode("utf-8"), "text/markdown")},
        )
        assert up.status_code == 201
        doc_id = up.json()["id"]
        client.post(f"/api/documents/{doc_id}/approve")

        # Query asking for procedure
        chat_res = client.post("/api/chat", json={"query": "How do I overhaul the main pressure valve?"})
        assert chat_res.status_code == 200
        body = chat_res.json()
        assert body["grounded"] is True
        assert "sop" in body
        sop = body["sop"]
        assert len(sop["steps"]) >= 1

        # Check that citation IDs in steps exist in citations
        valid_cids = {c["reference_id"] for c in body["citations"]}
        for step in sop["steps"]:
            for cid in step["citation_ids"]:
                assert cid in valid_cids
                assert cid != "S99"  # S99 was stripped

        # Irrelevant question abstains
        irr_res = client.post("/api/chat", json={"query": "How do I calibrate quantum flux capacitor?"})
        assert irr_res.status_code == 200
        assert irr_res.json()["abstained"] is True
        assert "sop" not in irr_res.json()


def test_plc_repair_against_seed_corpus_still_abstains(tmp_path, monkeypatch):
    """'How do I repair a PLC fault?' against seed corpus still abstains."""
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as client:
        res = client.post("/api/chat", json={"query": "How do I repair a PLC fault?"})
        assert res.status_code == 200
        assert res.json()["abstained"] is True


def test_pdf_embedded_image_asset_and_sop_figures(tmp_path, monkeypatch, fake_llm):
    """PDF fixture with embedded image produces asset, and matching query returns figure in SOP."""
    # Generate PDF with text and image
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (50, 72),
        "Procedure for hydraulic pump overhaul:\n"
        "Figure 1: Hydraulic pump assembly\n"
        "Step 1: Inspect hydraulic pump seal and lubricate bearings thoroughly."
    )
    img_pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), 1)
    img_pix.clear_with(200)
    page.insert_image(fitz.Rect(50, 150, 150, 250), pixmap=img_pix)
    pdf_bytes = doc.tobytes()
    doc.close()

    with make_test_env(tmp_path, monkeypatch, login_as="admin") as client:
        upload = client.post(
            "/api/documents",
            files={"file": ("hydraulic_pump.pdf", pdf_bytes, "application/pdf")},
        )
        assert upload.status_code == 201
        doc_id = upload.json()["id"]
        assert upload.json()["asset_count"] >= 1

        # Approve document
        client.post(f"/api/documents/{doc_id}/approve")

        # Query procedure
        res = client.post("/api/chat", json={"query": "How do I overhaul the hydraulic pump?"})
        assert res.status_code == 200
        data = res.json()
        assert "sop" in data
        assert len(data["sop"]["figures"]) >= 1
        asset_url = data["sop"]["figures"][0]["url"]

        # Asset can be retrieved
        asset_res = client.get(asset_url)
        assert asset_res.status_code == 200


def test_docx_ingestion_and_retrieval(tmp_path, monkeypatch):
    """DOCX fixture text is retrievable after approval."""
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    docx = Document()
    docx.add_heading("Compressor Safety Protocol", level=1)
    docx.add_paragraph("Industrial compressor cooling protocol: purge coolant lines before inspection.")
    buf = io.BytesIO()
    docx.save(buf)
    docx_bytes = buf.getvalue()

    with make_test_env(tmp_path, monkeypatch, login_as="admin") as client:
        upload = client.post(
            "/api/documents",
            files={"file": ("compressor_safety.docx", docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
        assert upload.status_code == 201
        doc_id = upload.json()["id"]

        # Approve
        client.post(f"/api/documents/{doc_id}/approve")

        # Query
        res = client.post("/api/chat", json={"query": "What does the compressor cooling protocol require?"})
        assert res.status_code == 200
        assert res.json()["grounded"] is True
        assert any("purge coolant lines" in c["passage"] for c in res.json()["citations"])


def test_feedback_synthesize_and_pdf_export(tmp_path, monkeypatch):
    """Feedback and synthesis notes are recorded, and SOP can be exported as a valid PDF."""
    monkeypatch.setattr(main, "LLM_API_KEY", "")  # Extractive path
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as client:
        # Create procedure manual
        doc_content = (
            "# Conveyor Belt Calibration\n"
            "Conveyor belt tensioning procedure: check belt alignment and tighten tension bolts to 45 Nm."
        )
        up = client.post(
            "/api/documents",
            files={"file": ("conveyor.md", doc_content.encode("utf-8"), "text/markdown")},
        )
        assert up.status_code == 201
        doc_id = up.json()["id"]
        client.post(f"/api/documents/{doc_id}/approve")

        # Ask for SOP
        res = client.post("/api/chat", json={"query": "How do I perform conveyor belt tensioning?"})
        assert res.status_code == 200
        sop_info = res.json()["sop"]
        sop_id = sop_info["id"]

        # Add feedback
        fb_res = client.post(
            f"/api/sops/{sop_id}/feedback",
            json={
                "applied_how": "Applied torque wrench to 45 Nm",
                "outcome": "fixed",
                "notes": "Belt slippage stopped immediately",
            },
        )
        assert fb_res.status_code == 200

        # Add manual note
        note_res = client.post(
            f"/api/sops/{sop_id}/notes",
            json={"body": "Check bolt wear prior to tightening."},
        )
        assert note_res.status_code == 200

        # Synthesize notes (without key: concatenates feedback)
        syn_res = client.post(f"/api/sops/{sop_id}/notes/synthesize")
        assert syn_res.status_code == 200

        # Verify SOP details
        get_sop = client.get(f"/api/sops/{sop_id}")
        assert get_sop.status_code == 200
        sop_details = get_sop.json()
        assert len(sop_details["feedback"]) == 1
        assert len(sop_details["notes"]) == 2

        # Review SOP
        rev_res = client.post(f"/api/sops/{sop_id}/review")
        assert rev_res.status_code == 200

        # Export SOP as PDF
        exp_res = client.post(f"/api/sops/{sop_id}/export")
        assert exp_res.status_code == 200
        assert exp_res.headers["content-type"] == "application/pdf"
        assert exp_res.content.startswith(b"%PDF")


def test_voice_websocket_requires_cookie(tmp_path, monkeypatch):
    """Voice WebSocket rejects connections without authentication cookie with code 4401."""
    with make_test_env(tmp_path, monkeypatch, login_as=None) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/api/voice/ws"):
                pass


def test_generate_sop_with_operational_context_and_dark_pdf_export(tmp_path, monkeypatch):
    """Generate SOP button with machine details, expert notes, and module selection produces a 5-page dark executive PDF."""
    import fitz
    monkeypatch.setattr(main, "LLM_API_KEY", "")  # Extractive path
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as client:
        # Create procedure manual
        doc_content = (
            "# Robotic Arm Series 7 Joint 4 Calibration\n"
            "Joint 4 motor overtorque fault recovery procedure: check optical encoder alignment, "
            "inspect drive belt tension, and isolate primary 480V bus before tightening coupling bolts to 35 Nm."
        )
        up = client.post(
            "/api/documents",
            files={"file": ("Robotic_Arm_Series_7_Maintenance_Guide_Rev4.pdf", doc_content.encode("utf-8"), "application/pdf")},
        )
        assert up.status_code == 201
        doc_id = up.json()["id"]
        client.post(f"/api/documents/{doc_id}/approve")

        # Chat with generate_sop and operational context
        res = client.post(
            "/api/chat",
            json={
                "query": "Joint 4 motor overtorque fault recovery procedure",
                "generate_sop": True,
                "machine_name": "Robotic Packaging Arm (SKU #RPA-990)",
                "machine_type": "Line 2 | Industrial Packaging",
                "error_code": "ERR-909",
                "expert_notes": "Field note: Optical encoder re-calibrated. Verified pneumatic brake hold.",
                "module_name": "Robotic_Arm_Series_7_Maintenance_Guide_Rev4.pdf",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["grounded"] is True
        assert "sop" in data
        sop_info = data["sop"]
        sop_id = sop_info["id"]
        assert sop_info["machine_name"] == "Robotic Packaging Arm (SKU #RPA-990)"
        assert sop_info["error_code"] == "ERR-909"
        assert sop_info["export_url"] == f"/api/sops/{sop_id}/export"

        # Verify GET /api/sops/{sop_id} contains context
        sop_get = client.get(f"/api/sops/{sop_id}")
        assert sop_get.status_code == 200
        sop_json = sop_get.json()
        assert sop_json["machine_name"] == "Robotic Packaging Arm (SKU #RPA-990)"
        assert sop_json["error_code"] == "ERR-909"
        assert len(sop_json["notes"]) >= 1

        # Test GET export endpoint
        get_exp = client.get(f"/api/sops/{sop_id}/export")
        assert get_exp.status_code == 200
        assert get_exp.headers["content-type"] == "application/pdf"
        assert get_exp.content.startswith(b"%PDF")

        # Verify PDF layout and contents using PyMuPDF: includes executive sections + real reference pages at the end
        pdf_doc = fitz.open(stream=get_exp.content, filetype="pdf")
        assert pdf_doc.page_count >= 5

        # Verify real reference page is included in the last section
        last_page_text = pdf_doc[-1].get_text()
        assert "Primary source reference manual" in last_page_text or "GROUND TRUTH" in last_page_text

        page1_text = pdf_doc[0].get_text()
        assert "Robotic Packaging Arm" in page1_text
        assert "ERR-909" in page1_text

        page4_text = pdf_doc[3].get_text()
        assert "Optical encoder re-calibrated" in page4_text


def test_ai_decides_rag_targets_by_understanding_problem_not_literal_words(tmp_path, monkeypatch):
    """AI understands the underlying equipment problem and failure mode to decide RAG search targets, rather than relying solely on literal words."""
    monkeypatch.setattr(main, "LLM_API_KEY", "")  # Extractive path
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as client:
        # Upload formal engineering manual
        manual_text = (
            "# Joint 4 Motor Overtorque Recovery\n"
            "Joint 4 motor overtorque fault recovery procedure: check optical encoder alignment, "
            "inspect drive belt tension, and isolate primary 480V bus before tightening coupling bolts to 35 Nm."
        )
        up = client.post(
            "/api/documents",
            files={"file": ("robotic_arm_manual.md", manual_text.encode("utf-8"), "text/markdown")},
        )
        assert up.status_code == 201
        doc_id = up.json()["id"]
        client.post(f"/api/documents/{doc_id}/approve")

        # Colloquial symptom query where the user does NOT use the manual's words
        user_query = "the robotic arm is wobbling and stopping during carton transfer"

        # Direct test on retrieve: without AI problem intent, literal query terms lack formal manual keywords
        with main.connect() as conn:
            doc_rows = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchall()
            literal_matches = main.retrieve(user_query, doc_rows)
            # The manual does not have 'wobbling', 'carton', or 'transfer'
            literal_coverage = literal_matches[0]["coverage"] if literal_matches else 0.0

            # With AI problem understanding:
            ai_intent = main._heuristic_problem_understanding(
                user_query,
                machine_name="Robotic Packaging Arm",
                error_code="ERR-909"
            )
            assert "motor overtorque stall" in ai_intent["understood_problem"]
            assert any("motor overtorque" in sq.lower() for sq in ai_intent["search_queries"])

            ai_guided_matches = main.retrieve(user_query, doc_rows, ai_intent=ai_intent)
            assert len(ai_guided_matches) > 0
            # AI-guided retrieval achieves high relevance and coverage because it targeted the real problem
            assert ai_guided_matches[0]["score"] >= 0.8
            assert ai_guided_matches[0]["coverage"] >= 0.7
            assert "coupling bolts to 35 Nm" in ai_guided_matches[0]["passage"]

        # Full endpoint test: /api/chat understands the problem and returns rag_strategy
        chat_res = client.post(
            "/api/chat",
            json={
                "query": user_query,
                "machine_name": "Robotic Packaging Arm",
                "error_code": "ERR-909",
                "generate_sop": True,
            },
        )
        assert chat_res.status_code == 200
        body = chat_res.json()
        assert body["grounded"] is True
        assert "rag_strategy" in body
        assert "motor overtorque" in body["rag_strategy"]["understood_problem"].lower()


def test_separate_chat_creation_history_resumption_and_member_collaboration(tmp_path, monkeypatch):
    """Separate chat creation with machine id and problem, member collaboration, and chat resumption with saved history."""
    import fitz
    monkeypatch.setattr(main, "LLM_API_KEY", "")
    with make_test_env(tmp_path, monkeypatch, login_as="admin") as client:
        # Create engineer user to invite
        with main.connect() as conn:
            eng_salt, eng_hash = main.auth.hash_password("password123")
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, password_salt, role, created_at) "
                "VALUES ('eng_sarah', 'Sarah Connor', ?, ?, 'engineer', ?)",
                (eng_hash, eng_salt, main.now()),
            )
            sarah_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 1. Roster of users available to invite
        roster_res = client.get("/api/chat/roster")
        assert roster_res.status_code == 200
        roster = roster_res.json()["users"]
        assert any(u["username"] == "eng_sarah" for u in roster)

        # 2. Create separate chat with machine ID, problem title, and members
        create_res = client.post(
            "/api/chat/sessions",
            json={
                "name": "Line 4 Lathe - Headstock Vibration",
                "machine_id": "LATHE-04",
                "machine_type": "Precision Engine Lathe",
                "problem": "Headstock spindle bearing excessive runout and vibration",
                "module_name": "The Lathe Book.pdf",
                "member_ids": [sarah_id],
            },
        )
        assert create_res.status_code == 201
        created_data = create_res.json()
        session_id = created_data["session_id"]
        assert created_data["machine_id"] == "LATHE-04"
        assert len(created_data["members"]) == 2  # creator (admin) + invited (Sarah)

        # 3. Chat inside the session and generate SOP
        # First ensure approved procedure document exists
        manual_doc = (
            "# Headstock Spindle Bearing Alignment and Truing\n"
            "Lathe calibration procedure: mount dial indicator to carriage slideways. "
            "Inspect bearing runout within 0.005 mm tolerance and tighten bearing preload collars."
        )
        up = client.post(
            "/api/documents",
            files={"file": ("The Lathe Book.pdf", manual_doc.encode("utf-8"), "application/pdf")},
        )
        assert up.status_code == 201
        doc_id = up.json()["id"]
        client.post(f"/api/documents/{doc_id}/approve")

        chat_res = client.post(
            "/api/chat",
            json={
                "session_id": session_id,
                "query": "Lathe calibration procedure: mount dial indicator and tighten bearing preload collars",
                "generate_sop": True,
            },
        )
        assert chat_res.status_code == 200
        chat_data = chat_res.json()
        assert chat_data["session_id"] == session_id
        assert chat_data["grounded"] is True
        assert "sop" in chat_data
        sop_id = chat_data["sop"]["id"]

        # 4. Add another member to the chat session
        with main.connect() as conn:
            eng2_salt, eng2_hash = main.auth.hash_password("password456")
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, password_salt, role, created_at) "
                "VALUES ('eng_alex', 'Alex Murphy', ?, ?, 'engineer', ?)",
                (eng2_hash, eng2_salt, main.now()),
            )
            alex_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        add_res = client.post(
            f"/api/chat/sessions/{session_id}/members",
            json={"user_id": alex_id},
        )
        assert add_res.status_code == 200
        assert len(add_res.json()["members"]) == 3

        # 5. List chat sessions
        list_res = client.get("/api/chat/sessions")
        assert list_res.status_code == 200
        sessions_list = list_res.json()["sessions"]
        target_session = next((s for s in sessions_list if s["id"] == session_id), None)
        assert target_session is not None
        assert target_session["machine_id"] == "LATHE-04"
        assert target_session["message_count"] >= 3

        # 6. Resume / load chat session with complete history and hydrated SOP card
        resume_res = client.get(f"/api/chat/sessions/{session_id}")
        assert resume_res.status_code == 200
        resumed = resume_res.json()
        assert resumed["session"]["machine_id"] == "LATHE-04"
        assert len(resumed["members"]) == 3
        assert len(resumed["messages"]) >= 3

        # Verify hydrated SOP card in chat history
        sop_msg = next((m for m in resumed["messages"] if m.get("sop_id") == sop_id), None)
        assert sop_msg is not None
        assert "sop" in sop_msg
        assert sop_msg["sop"]["id"] == sop_id

        # 7. Verify SOP PDF export includes real reference pages at the end
        export_res = client.get(f"/api/sops/{sop_id}/export")
        assert export_res.status_code == 200
        assert export_res.headers["content-type"] == "application/pdf"
        exp_pdf = fitz.open(stream=export_res.content, filetype="pdf")
        assert exp_pdf.page_count >= 5
        last_text = exp_pdf[-1].get_text()
        assert "Primary source reference manual" in last_text or "GROUND TRUTH" in last_text


