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
- Live dashboard metrics, query outcome charts, CSV export, and local escalation tickets.
- Browser speech dictation and read-aloud where supported.
- Persistent text and voice sessions in SQLite. Text turns reuse the returned
  `session_id`; Live voice sessions return a `sessionInfo` event and store typed
  input, Gemini input/output transcriptions, and knowledge-tool results.

## Run On Windows

Install Python 3.10 or later, then double-click `run.bat`. It creates a virtual environment, installs dependencies, and starts the app at `http://127.0.0.1:8000`.

Manual setup:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. Interactive API documentation is at `http://127.0.0.1:8000/docs`.

A live audio chat sample is available at `http://127.0.0.1:8000/live`. It uses the Gemini Live API with model `gemini-3.8-live` for bidirectional voice. The browser connects to a same-origin backend WebSocket relay; the permanent `GEMINI_API_KEY` never leaves the server. Open the page through the running app rather than directly as a local file, and restart Uvicorn after backend changes.

To enable optional model summaries, copy `.env.example` to `.env` and set `LLM_API_KEY`, `LLM_API_BASE_URL`, and `LLM_MODEL`. Never commit API keys, and never embed them in the HTML files. When no key is set, or the provider fails, the backend returns source excerpts in extractive mode. Configured provider calls send the question and retrieved excerpts to that provider; do not send confidential production documents to an unapproved service.

## Demo Flow

1. Ask “What does the lexical retrieval score mean?” and inspect the exact source passage and relevance score.
2. Ask “How do I repair a PLC fault?” and show the safety abstention.
3. Upload a text-based PDF, TXT, or Markdown file, observe that it is pending, then approve it in the local demo reviewer flow and query it.
4. Log an escalation from an abstained response, then open the supervisor dashboard and export the query log.
5. Ask "hi" to show that a conversational message is answered, while an operational request is still refused.

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
```

The API is same-origin and does not enable cross-origin access. Main endpoints:
`GET /api/health`, `GET /api/documents`, `POST /api/documents`,
`POST /api/documents/{id}/approve`, `POST /api/chat`, `POST /api/sessions`,
`GET /api/sessions/{session_id}`, `POST /api/escalations`, and
`GET /api/analytics`. The Live relay is available at `/api/voice/ws`; pass a
previous voice `session_id` as a query parameter to continue that session.

## Important Limits

This build is a local prototype: reviewer approval is not authenticated; there is no multi-user identity, authorization, encryption-at-rest, audit-grade retention, malware scanning, OCR, CAD/image understanding, plant integration, external alert delivery, or certified safety validation. Scanned PDFs are not searchable. The operational-intent filter and model prompt are defense-in-depth, not a certified safety barrier. The retrieval relevance score is a lexical relevance measure only, not a confidence or correctness probability. Do not deploy it for production or use it to direct real equipment work.

Tailwind CSS, Chart.js, and icons currently load from CDNs, so full styling and charts require an internet connection. For a production or offline deployment, bundle these assets locally and replace the Tailwind Play CDN with a compiled stylesheet.

## Verification

```powershell
python -m pytest -q
```
