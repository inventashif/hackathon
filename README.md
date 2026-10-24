# NexusSOP

**Evidence-first knowledge assistant for industrial teams.** NexusSOP searches approved source documents, returns verbatim citations, and abstains on operational or safety-critical requests. It is a hackathon demonstration, not an approved plant system.

## What Works

- FastAPI backend with persistent SQLite document, query, and escalation records.
- Lexical passage retrieval with Porter stemming, IDF relevance scores, query-term coverage, and source excerpts.
- Every answerable question runs retrieval first; the retrieved passages are sent to the model as numbered references (`[S1]`, `[S2]`, ...) and the answer is checked so it can never cite a source that was not retrieved.
- Optional OpenAI-compatible grounded summarization for non-operational questions; extractive mode works without an API key. Transient provider failures are retried with backoff.
- Conversational replies ("hi", "thanks", "what can you do") are answered instead of refused, and are logged separately from evidence-grounded answers.
- Safety-intent refusal and low-evidence abstention. The refusal check looks at the object of the
  request, not just keywords, so "How do I configure the machine?" is refused while "How do I
  configure the dashboard?" is answered.
- PDF, TXT, and Markdown ingestion with human-review gating before retrieval.
- PNG, JPG, WEBP, BMP, and TIFF image ingestion with OCR text extraction and
  human-review gating before retrieval.
- Live dashboard metrics, query outcome charts, CSV export, and local escalation tickets.
- Browser speech dictation and read-aloud where supported.
- Persistent text and voice sessions in SQLite. Text turns reuse the returned
  `session_id`; Live voice sessions return a `sessionInfo` event and store typed
  input, Gemini input/output transcriptions, and knowledge-tool results.
- Gemini Live server-side knowledge retrieval through the approved-document
  `search_knowledge_base` tool. The Gemini API key stays on the backend.

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
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

### Windows PowerShell

```powershell
git clone https://github.com/inventashif/hackathon.git
cd hackathon
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Alternatively, Windows users can double-click `run.bat`; Linux/macOS users can
run `./run.sh`. Open `http://127.0.0.1:8000`. Interactive API documentation is
at `http://127.0.0.1:8000/docs`.

### Environment variables

`.env.example` contains the complete configuration:

| Variable | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Required for Gemini Live voice chat; leave blank if voice is not needed. |
| `GEMINI_LIVE_MODEL` | Gemini Live model; defaults to `gemini-3.8-live`. |
| `LLM_API_KEY` | NVIDIA NIM API key for generated summaries; optional because local extractive RAG works without it. |
| `LLM_API_BASE_URL` | OpenAI-compatible NVIDIA NIM endpoint; `https://integrate.api.nvidia.com/v1`. |
| `LLM_MODEL` | NVIDIA model used by this project: `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`. |
| `SOP_DB_PATH` | Optional SQLite path; defaults to `data/nexus_sop.sqlite3`. |

Without `LLM_API_KEY`, the app still works using local extractive RAG. The
Gemini key is only used by the backend WebSocket relay and is never exposed to
the browser. Do not send confidential documents to an unapproved external
model provider.

## Demo Flow

1. Ask “What does the lexical retrieval score mean?” and inspect the exact source passage and relevance score.
2. Ask “How do I repair a PLC fault?” and show the safety abstention.
3. Upload a text-based PDF, TXT, or Markdown file, observe that it is pending, then approve it in the local demo reviewer flow and query it.
4. Upload an image, let OCR index it, approve it, and ask a question grounded in the extracted text.
5. Open `/live`, start Gemini Live voice, and ask about an approved document to exercise the server-side knowledge tool.
6. Log an escalation from an abstained response, then open the supervisor dashboard and export the query log.
7. Ask "hi" to show that a conversational message is answered, while an operational request is still refused.

The bundled documents in `data/knowledge/` are synthetic demonstration content. Use only the supplied safe prompts during a presentation; do not present the sample sources as real plant manuals.

## Architecture

```text
Browser UI (existing HTML + Tailwind CDN)
        | same-origin JSON / multipart requests
FastAPI (backend/main.py)
        | retrieval (backend/stemmer.py + lexical scoring)
        | -> retrieved passages sent to the model as [S1]..[Sn]
        | -> answer checked so it only cites retrieved passages
SQLite (data/nexus_sop.sqlite3) + Markdown seed corpus
        | sessions, messages, documents, queries, analytics, escalations
Gemini Live browser audio/text
        | same-origin WebSocket relay (server-side tool execution)
        | -> approved-document retrieval + OCR/text excerpts
```

The API is same-origin and does not enable cross-origin access. Main endpoints:
`GET /api/health`, `GET /api/documents`, `POST /api/documents`,
`POST /api/voice/knowledge`,
`POST /api/documents/{id}/approve`, `POST /api/chat`, `POST /api/sessions`,
`GET /api/sessions/{session_id}`, `POST /api/escalations`, and
`GET /api/analytics`, and `GET /api/analytics/export`. The Live relay is
available at `/api/voice/ws`; pass a previous voice `session_id` as a query
parameter to continue that session. Live pages are available at `/live`,
`/live-audio`, and `/voice-chat`.

## Important Limits

This build is a local prototype: reviewer approval is not authenticated; there is no multi-user identity, authorization, encryption-at-rest, audit-grade retention, malware scanning, CAD/image understanding, plant integration, external alert delivery, or certified safety validation. OCR quality depends on the local Tesseract installation, and scanned PDFs are not searchable. The operational-intent filter and model prompt are defense-in-depth, not a certified safety barrier. The retrieval relevance score is a lexical relevance measure only, not a confidence or correctness probability. Do not deploy it for production or use it to direct real equipment work.

Tailwind CSS, Chart.js, and icons currently load from CDNs, so full styling and charts require an internet connection. For a production or offline deployment, bundle these assets locally and replace the Tailwind Play CDN with a compiled stylesheet.

## Verification

```bash
python -m pytest -q
```
