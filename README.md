# MechTech AI

**Evidence-first knowledge assistant with multi-user accounts, shared chat, and SOP generation.** MechTech AI searches approved source documents, returns verbatim citations, generates source-grounded procedures with original figures, and abstains on operational or safety-critical requests. It is a hackathon demonstration, not an approved plant system.

## What Works

- **Multi-user authentication** with PBKDF2 password hashing, session cookies, and role-based access control (admin/engineer).
- **Shared chat room** with real-time polling, allowing multiple users to collaborate in the same conversation.
- **SOP generation** from approved manuals with numbered steps, citations, and original extracted figures.
- **Feedback and notes** on SOPs, including engineer-synthesized notes from collective experience.
- **PDF export** of SOPs with steps, figures, feedback, and notes.
- **Invite system** for engineers to join via single-use codes (no email required).
- **Document approval workflow** with approve, reject, and delete actions (admin-only).
- **Enhanced ingestion**: PDF with page markers, embedded image extraction, DOCX support, and page snapshots for vector diagrams.
- **Lexical passage retrieval** with Porter stemming, IDF relevance scores, query-term coverage, and source excerpts.
- Every answerable question runs retrieval first; the retrieved passages are sent to the model as numbered references (`[S1]`, `[S2]`, ...) and the answer is checked so it can never cite a source that was not retrieved.
- Optional NVIDIA NIM grounded summarization for non-operational questions; extractive mode works without an API key. Transient provider failures are retried with backoff.
- Conversational replies ("hi", "thanks", "what can you do") are answered instead of refused, and are logged separately from evidence-grounded answers.
- Safety-intent refusal and low-evidence abstention. The refusal check looks at the object of the request, not just keywords, so "How do I configure the machine?" is refused while "How do I configure the dashboard?" is answered.
- PDF, DOCX, TXT, and Markdown ingestion with human-review gating before retrieval.
- PNG, JPG, WEBP, BMP, and TIFF image ingestion with OCR text extraction and human-review gating before retrieval.
- Live dashboard metrics, query outcome charts, CSV export, and local escalation tickets.
- Browser speech dictation and read-aloud where supported.
- Persistent text and voice sessions in SQLite. Text turns reuse the returned `session_id`; Live voice sessions return a `sessionInfo` event and store typed input, Gemini input/output transcriptions, and knowledge-tool results.
- Gemini Live server-side knowledge retrieval through the approved-document `search_knowledge_base` tool. The Gemini API key stays on the backend.

## Setup

Install Python 3.10 or later, then create a local environment file. Never
commit `.env`; every developer should use their own API keys.

### Linux/macOS

```bash
git clone https://github.com/inventashif/hackathon.git
cd hackathon
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

### Windows PowerShell

```powershell
git clone https://github.com/inventashif/hackathon.git
cd hackathon
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Alternatively, Windows users can double-click `run.bat`; Linux/macOS users can
run `./run.sh`. The launchers generate a local HTTPS certificate when OpenSSL
is available and start at `https://localhost:8000`. Accept the one-time
certificate warning, then allow microphone access. Install `mkcert` for a
trusted local certificate without that warning. For LAN voice access, use a
certificate trusted for the machine hostname/IP; the frontend automatically
uses `wss://`. If certificate generation is unavailable, use
`http://localhost:8000`; plain HTTP on a LAN IP cannot access the microphone.
The launcher prints the detected LAN URL when it starts.
Interactive API documentation is available at the same address followed by
`/docs`.

### Environment variables

`.env.example` contains the complete configuration:

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Required for Gemini Live voice chat; leave blank if voice is not needed. |
| `GEMINI_LIVE_MODEL` | Gemini Live model; defaults to `gemini-3.8-live`. |
| `LLM_API_KEY` | NVIDIA NIM API key for generated summaries; optional because local extractive RAG works without it. |
| `LLM_API_BASE_URL` | NVIDIA NIM chat endpoint; `https://integrate.api.nvidia.com/v1`. |
| `LLM_MODEL` | NVIDIA model used by this project: `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`. |
| `SOP_DB_PATH` | Optional SQLite path; defaults to `data/nexus_sop.sqlite3`. |
| `SOP_ADMIN_USER` | Optional admin username; defaults to `admin` on first boot. |
| `SOP_ADMIN_PASSWORD` | Optional admin password; defaults to `admin` on first boot. |
| `SESSION_EXPIRY_DAYS` | Session cookie expiry in days; defaults to 14. |
| `COOKIE_SECURE` | Set to `true` for HTTPS-only cookies; defaults to `false` for local development. |
| `SOP_SSL_CERTFILE` | Optional path to the HTTPS certificate passed to Uvicorn. |
| `SOP_SSL_KEYFILE` | Optional path to the matching HTTPS private key passed to Uvicorn. |

On first boot, if no users exist, the system creates a default admin account (`admin`/`admin`). You should change this password immediately in the Admin view or via the `SOP_ADMIN_USER` and `SOP_ADMIN_PASSWORD` environment variables.

Without `LLM_API_KEY`, the app still works using local extractive RAG. The
Gemini key is only used by the backend WebSocket relay and is never exposed to
the browser. Do not send confidential documents to an unapproved external
model provider.

## Docker

Docker is the recommended setup when you want the same runtime on every
machine. Clone the public repository first, create `.env` locally, and pass
the keys to the container at runtime. The keys are not copied into the image
or committed to Git.

### Linux/macOS

```bash
git clone https://github.com/inventashif/hackathon.git
cd hackathon
cp .env.example .env
# Edit `.env` and add `LLM_API_KEY` for NVIDIA NIM and `GEMINI_API_KEY` for Google Gemini.
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:8000/api/health
```

Open `http://127.0.0.1:8000`. Stop the container with:

```bash
docker compose down
```

### Windows PowerShell

```powershell
git clone https://github.com/inventashif/hackathon.git
cd hackathon
Copy-Item .env.example .env
# Edit `.env` and add `LLM_API_KEY` for NVIDIA NIM and `GEMINI_API_KEY` for Google Gemini.
docker compose up --build -d
docker compose ps
Invoke-WebRequest http://127.0.0.1:8000/api/health
```

Stop the container with:

```powershell
docker compose down
```

The compose file mounts `./data` so SQLite sessions, approved documents, and
uploaded images persist across container restarts. `tesseract-ocr` is installed
inside the image for image OCR. To follow logs, run
`docker compose logs -f nexussop`.

## Demo Flow

1. **First login**: Sign in with the default admin account (`admin`/`admin`). You'll see a prompt to change your password.
2. **Upload and approve**: Upload a PDF or DOCX document, observe that it is pending, then approve it in the Admin view.
3. **SOP generation**: Ask a procedure question like "How do I perform a safety check?" with sufficient evidence in approved documents. The system will generate an SOP with steps and figures.
4. **Shared chat**: Open the app in a second browser window and log in with a new engineer account (create via Invite). Messages appear in both windows.
5. **Feedback**: Add feedback to a generated SOP and synthesize a note from collective experience.
6. **Export**: Export an SOP as PDF with steps, figures, feedback, and notes.
7. **Live voice**: Open `/live`, start Gemini Live voice, and ask about an approved document to exercise the server-side knowledge tool.
8. **Analytics**: View the supervisor dashboard with query metrics and document approval queue.

The bundled documents in `data/knowledge/` are synthetic demonstration content. Use only the supplied safe prompts during a presentation; do not present the sample sources as real plant manuals.

## Architecture

```text
Browser UI (existing HTML + Tailwind CDN)
        | same-origin JSON / multipart requests + session cookies
FastAPI (backend/main.py)
        | auth (backend/auth.py) - PBKDF2 hashing, sessions, roles, invites
        | ingest (backend/ingest.py) - PDF/DOCX with page markers & figure extraction
        | sop (backend/sop.py) - SOP generation, feedback, notes, PDF export
        | retrieval (backend/stemmer.py + lexical scoring, up to 12 passages for SOP)
        | -> retrieved passages sent to the model as [S1]..[Sn]
        | -> answer checked so it only cites retrieved passages
SQLite (data/nexus_sop.sqlite3) + Markdown seed corpus
        | users, sessions, session_messages, rooms, room_members
        | documents, document_assets, queries, sops, sop_feedback, sop_notes
        | escalations, analytics
Gemini Live browser audio/text
        | same-origin WebSocket relay (server-side tool execution)
        | -> approved-document retrieval + OCR/text excerpts
```

The API is same-origin and does not enable cross-origin access. Main endpoints:
`GET /api/health`, `GET /api/auth/me`, `POST /api/auth/login`, `POST /api/auth/logout`,
`GET /api/users`, `POST /api/users`, `POST /api/users/{id}/disable`, `POST /api/users/{id}/enable`,
`GET /api/documents`, `POST /api/documents`, `POST /api/documents/{id}/approve`,
`POST /api/documents/{id}/reject`, `DELETE /api/documents/{id}`,
`POST /api/chat`, `GET /api/rooms/shared`, `GET /api/rooms/shared/messages`,
`POST /api/invites`, `GET /api/invites/{code}`, `POST /api/invites/{code}/accept`,
`GET /api/sops/{id}`, `POST /api/sops/{id}/feedback`, `POST /api/sops/{id}/notes`,
`POST /api/sops/{id}/notes/synthesize`, `POST /api/sops/{id}/export`,
`GET /api/assets/{id}`, `POST /api/voice/knowledge`, `POST /api/sessions`,
`GET /api/sessions/{session_id}`, `POST /api/escalations`, `GET /api/analytics`,
and `GET /api/analytics/export`. The Live relay is available at `/api/voice/ws`;
pass a previous voice `session_id` as a query parameter to continue that session.
Live pages are available at `/live`, `/live-audio`, and `/voice-chat`.

## Important Limits

This build is a local prototype: while it now has multi-user authentication and document approval gating, it lacks encryption-at-rest, audit-grade retention, malware scanning, CAD/image understanding, plant integration, external alert delivery, or certified safety validation. OCR quality depends on the local Tesseract installation, and scanned PDFs are not searchable. The operational-intent filter and model prompt are defense-in-depth, not a certified safety barrier. The retrieval relevance score is a lexical relevance measure only, not a confidence or correctness probability. Do not deploy it for production or use it to direct real equipment work.

Tailwind CSS, Chart.js, and icons currently load from CDNs, so full styling and charts require an internet connection. For a production or offline deployment, bundle these assets locally and replace the Tailwind Play CDN with a compiled stylesheet.

## Verification

```bash
python -m pytest -q
```
