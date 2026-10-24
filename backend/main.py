from __future__ import annotations

import csv
import io
import json
import math
import os
import asyncio
import re
import sqlite3
import subprocess
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx
import websockets
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from pypdf import PdfReader
from dotenv import load_dotenv

from backend.stemmer import stem as _stemmer


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
KNOWLEDGE_DIR = ROOT / "data" / "knowledge"
DB_PATH = Path(os.environ.get("SOP_DB_PATH", ROOT / "data" / "nexus_sop.sqlite3"))
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_API_BASE_URL = os.environ.get(
    "LLM_API_BASE_URL", "https://integrate.api.nvidia.com/v1"
).rstrip("/")
LLM_MODEL = os.environ.get(
    "LLM_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_LIVE_MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.8-live")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MIN_RETRIEVAL_SCORE = 0.12
MIN_EVIDENCE_COVERAGE = 0.5
CONVERSATIONAL_REPLY = (
    "Hello! I'm the NexusSOP evidence assistant. Ask me about document review, "
    "evidence policy, or the system guide, and I'll answer with cited source excerpts."
)

IMAGE_UPLOAD_DIR = ROOT / "data" / "uploads"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}

DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".md"} | IMAGE_EXTENSIONS


def ocr_image(image_path: Path) -> str:
    """Extract text from an image using tesseract OCR.

    Returns the extracted text, or an empty string if OCR is unavailable
    or produces no output.
    """
    try:
        result = subprocess.run(
            ["tesseract", str(image_path), "stdout"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def prepare_image_for_storage(file_bytes: bytes, name: str) -> tuple[Path, str]:
    """Validate an image, convert to PNG, and save to the upload directory.

    Returns the saved image path and its OCR-extracted text.

    The returned body includes the sanitized filename stem so that the image
    is searchable by its name (e.g. searching "astra" finds astra.png).
    """
    IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.open(io.BytesIO(file_bytes))
    img.verify()
    img = Image.open(io.BytesIO(file_bytes))
    # Normalize to RGB (handles palette, CMYK, RGBA, etc.)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    # --- OCR preprocessing: upscale 2x + grayscale + autocontrast ---
    # Improves tesseract accuracy on screenshots, photos, and low-res scans.
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    if img.mode != "L":
        img = img.convert("L")
    img = ImageOps.autocontrast(img)
    safe_name = "".join(c for c in name if c.isalnum() or c in ".-_") or "image"
    stem = Path(safe_name).stem
    out_path = IMAGE_UPLOAD_DIR / f"{stem}.png"
    # Avoid overwriting existing files
    counter = 1
    while out_path.exists():
        out_path = IMAGE_UPLOAD_DIR / f"{stem}_{counter}.png"
        counter += 1
    img.save(out_path, format="PNG")
    ocr_text = ocr_image(out_path)
    # Prepend filename keywords so the image is retrievable by its name.
    body = f"[Image: {stem}]\n{ocr_text}".strip()
    return out_path, body
STOP_WORDS = {
    "a", "about", "after", "all", "an", "and", "are", "as", "at", "be", "by",
    "can", "do", "does", "for", "from", "how", "i", "in", "is", "it", "me",
    "of", "on", "or", "should", "the", "this", "to", "what", "when", "where",
    "which", "who", "with", "you", "your",
}
# --- Operational intent detection -------------------------------------------
# A flat keyword match refused harmless questions such as "What does the
# system guide say about startup?" or "How do I upload a document?". The
# filter is therefore split into three questions:
#
#   1. Is the request asking for work on plant equipment?  -> refuse
#   2. Is it a lookup question about the knowledge base?   -> answer
#   3. Otherwise, is it asking for step-by-step work?      -> refuse
#
# The model prompt independently forbids procedural answers, so this filter is
# a first-pass screen rather than the only barrier.

# Verbs that only ever describe physical work on plant equipment. Always
# refused, even when they appear inside an otherwise harmless sentence.
STRONG_OPERATIONAL_INTENT = re.compile(
    r"\b(lock\s*out|tag\s*out|loto|bypass\w*|override\w*|"
    r"de[- ]?energiz\w*|energiz\w*|calibrat\w*|torque|purge|prime|vent|"
    r"tighten|loosen|disassembl\w*|lubricat\w*)\b",
    re.IGNORECASE,
)

# Nouns that name a specific plant asset. A request naming one of these is a
# work request however the sentence is phrased.
EQUIPMENT_REFERENCE = re.compile(
    r"\b(machine|equipment|valve|pump|motor|controller|plc|compressor|"
    r"turbine|boiler|furnace|kiln|conveyor|robot|actuator|sensor|bearing|"
    r"generator|transformer|switchgear|breaker|gearbox|extruder|centrifuge|"
    r"manifold|vessel|tank|pipeline|rig|welder|crane|forklift|plant|"
    r"reactor|interlock|valve[- ]?\d+)\b",
    re.IGNORECASE,
)

# Words that are operational only in context. "reset", "steps", "configure",
# "install" and "startup" are all common in non-operational questions.
WEAK_OPERATIONAL_INTENT = re.compile(
    r"\b(repair\w*|replace(?:ment|d)?|maintain\w*|troubleshoot\w*|"
    r"reset\w*|restart\w*|wire\w*|voltage|pressure|procedure\w*|steps?|"
    r"fault\w*|error\s*code\w*|configure\w*|commission\w*|"
    r"start[- ]?up|startup|shut[- ]?down|shutdown|install\w*|"
    r"diagnos(?:e|is|ing)|program(?:ming|med)?|set[- ]?up|operat\w*|"
    r"isolat\w*|adjust(?:ment|ing)?|"
    r"work\s*instruction\w*|operational\s*procedure\w*)\b",
    re.IGNORECASE,
)

# Vocabulary of the knowledge base itself. Questions about these are answered
# from the indexed sources rather than refused.
KB_TOPIC_REFERENCE = re.compile(
    r"\b(document\w*|polic\w*|guide|manual|sop\w*|excerpt\w*|citation\w*|"
    r"knowledge|vault|retriev\w*|passage\w*|dashboard|analytic\w*|"
    r"threshold|upload\w*|approv\w*|review\w*|score\w*|source\w*|"
    r"index\w*|evidence|escalat\w*)\b",
    re.IGNORECASE,
)

# "Which document covers X?" / "What does the guide say about Y?" are lookup
# questions about the corpus, not requests for work instructions.
KB_LOOKUP_QUESTION = re.compile(
    r"^\s*(which|what)\b.*\b(document\w*|passage\w*|excerpt\w*|source\w*|"
    r"polic\w*|guide|manual|sop\w*|index\w*|evidence)\b",
    re.IGNORECASE,
)

# Explicit requests for step-by-step work.
INSTRUCTION_REQUEST = re.compile(
    r"\b(how\s+(?:do|can|would|should)\s+(?:i|we|you)|how\s+to|"
    r"steps?\s+(?:to|for|of|do)|procedure\s+(?:to|for|of)|"
    r"give\s+me|show\s+me|walk\s+me|teach\s+me|instructions?\s+(?:for|to|on)|"
    r"what(?:'s|\s+is)\s+the\s+(?:procedure|process|steps))\b",
    re.IGNORECASE,
)

# Words that turn a lookup question into a request for procedures.
PROCEDURE_REQUEST = re.compile(
    r"\b(steps?|procedure|instructions?|process|work\s*sheet|task\s*list)\b",
    re.IGNORECASE,
)

GREETING_PATTERN = re.compile(
    r"^\s*(hi|hello|hey|hiya|howdy|greetings|good\s+(morning|afternoon|evening)|"
    r"thanks|thank\s+you|what\s+can\s+you\s+do|help|who\s+are\s+you|"
    r"what\s+is\s+this|what\s+are\s+you)\b",
    re.IGNORECASE,
)



# Splits the phrase after an operational verb at the first preposition so the
# direct object can be identified ("install a document in the vault" -> "a
# document").
OBJECT_BREAK = re.compile(
    r"\b(?:in|into|for|to|of|on|with|from|at|by|and|or|that|which|when)\b",
    re.IGNORECASE,
)


def _verb_object(text: str, verb_end: int) -> str:
    """Return the direct object phrase that follows an operational verb."""
    tail = text[verb_end:]
    tail = tail.lstrip(" ,.:;")
    match = OBJECT_BREAK.search(tail)
    if match:
        tail = tail[: match.start()]
    return tail.strip(" ,.:;")


def is_operational_request(query: str) -> bool:
    """Decide whether a question must be refused as an operational request.

    The previous implementation was a flat keyword match, so it refused
    harmless questions such as "What does the system guide say about startup?"
    or "How do I upload a document?". The rules below separate three cases:

    * work on plant equipment            -> refuse
    * a lookup question about the corpus  -> answer from the sources
    * a request for step-by-step work     -> refuse

    The wording of the verb alone is not enough. "How do I install a document
    in the vault?" and "How do I configure the machine?" share a structure and
    only the object tells them apart.
    """
    text = query.strip()
    if not text:
        return False

    # 1. Physical work verbs are always a work request.
    if STRONG_OPERATIONAL_INTENT.search(text):
        return True

    # 2. No operational wording at all -> an ordinary question.
    verb = WEAK_OPERATIONAL_INTENT.search(text)
    if not verb:
        return False

    object_phrase = _verb_object(text, verb.end())
    object_equipment = EQUIPMENT_REFERENCE.search(object_phrase)
    object_kb_topic = KB_TOPIC_REFERENCE.search(object_phrase)
    equipment = EQUIPMENT_REFERENCE.search(text)
    lookup = KB_LOOKUP_QUESTION.search(text)

    # 3. The object of the verb decides the case. Work on a named asset is
    #    refused; work on the knowledge base itself is answerable.
    if object_equipment:
        return True
    if object_kb_topic and not equipment:
        return False

    # 4. A lookup about a specific document is a corpus question. It stays
    #    answerable unless it also asks for the procedure of some asset.
    if lookup and not (equipment and PROCEDURE_REQUEST.search(text)):
        return False

    # 5. Work on a named asset anywhere in the sentence is a work request,
    #    even when the wording is framed as a question ("How do I configure
    #    the machine?").
    if equipment:
        return True

    # 6. Operational wording aimed at the knowledge base ("How do I reset the
    #    retrieval threshold?") is answerable.
    if KB_TOPIC_REFERENCE.search(text) and not INSTRUCTION_REQUEST.search(text):
        return False

    # 7. Anything else that asks for steps is a work request.
    if INSTRUCTION_REQUEST.search(text):
        return True

    return False


class EscalationRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    reason: str = Field(min_length=3, max_length=500)


class VoiceKnowledgeRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)


class SessionRequest(BaseModel):
    kind: str = Field(default="text", pattern="^(text|voice)$")


class ChatRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    document_id: int | None = None
    image: str | None = None
    audio: str | None = None
    session_id: str | None = None


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                body TEXT NOT NULL,
                source TEXT NOT NULL,
                image_path TEXT,
                approved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS queries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                grounded INTEGER NOT NULL,
                document_id INTEGER,
                retrieval_score REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id)
            );
            CREATE TABLE IF NOT EXISTS escalations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('text', 'voice')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'tool', 'system')),
                content TEXT NOT NULL,
                modality TEXT NOT NULL DEFAULT 'text',
                created_at TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
            """
        )
        # Migration: track the outcome kind so conversational replies are not
        # counted as evidence-grounded answers in the analytics dashboard.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(queries)")}
        if "mode" not in columns:
            connection.execute(
                "ALTER TABLE queries ADD COLUMN mode TEXT NOT NULL DEFAULT 'grounded'"
            )
            connection.execute(
                "UPDATE queries SET mode = "
                "CASE WHEN grounded = 1 THEN 'grounded' ELSE 'abstained' END"
            )

        # Migration: add image_path column to documents for image uploads.
        doc_columns = {row["name"] for row in connection.execute("PRAGMA table_info(documents)")}
        if "image_path" not in doc_columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN image_path TEXT"
            )

        document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if document_count == 0:
            for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
                connection.execute(
                    "INSERT INTO documents (name, body, source, approved, created_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (path.name, path.read_text(encoding="utf-8"), "Demo knowledge base", now()),
                )


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_session(kind: str) -> str:
    session_id = str(uuid.uuid4())
    timestamp = now()
    with connect() as connection:
        connection.execute(
            "INSERT INTO sessions (id, kind, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, kind, timestamp, timestamp),
        )
    return session_id


def ensure_session(session_id: str | None, kind: str) -> str:
    if session_id:
        with connect() as connection:
            row = connection.execute(
                "SELECT id FROM sessions WHERE id = ? AND kind = ?",
                (session_id, kind),
            ).fetchone()
        if row:
            return session_id
    return create_session(kind)


def log_session_message(
    session_id: str, role: str, content: str, modality: str = "text"
) -> None:
    content = content.strip()
    if not content:
        return
    with connect() as connection:
        connection.execute(
            "INSERT INTO session_messages "
            "(session_id, role, content, modality, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, modality, now()),
        )
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now(), session_id),
        )


def _stem(word: str) -> str:
    return _stemmer(word)


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    result: list[str] = []
    seen: set[str] = set()
    for word in words:
        if word in STOP_WORDS:
            continue
        token = _stem(word)
        if token not in seen:
            seen.add(token)
            result.append(token)
    return result


HEADING_PATTERN = re.compile(r"^#{1,6}\s+.*$")


def split_passages(text: str) -> list[str]:
    """Split a document into retrievable passages.

    A Markdown heading is kept with the section it introduces. Splitting it off
    on its own produced title-only passages such as "# Document review and
    escalation", which then ranked as the best citation for broad queries even
    though they contain no usable evidence.
    """
    passages: list[str] = []
    pending_heading = ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if HEADING_PATTERN.match(block) and "\n" not in block:
            pending_heading = block
            continue
        if pending_heading:
            block = f"{pending_heading}\n{block}"
            pending_heading = ""
        if len(block) <= 1200:
            passages.append(block)
            continue
        passages.extend(block[index:index + 1000] for index in range(0, len(block), 900))
    if pending_heading:
        passages.append(pending_heading)
    return passages


def retrieve(query: str, documents: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Score every passage in the approved corpus against the query.

    Two signals are produced for each candidate:

    * ``score``     - IDF-weighted share of the *answerable* query terms covered.
    * ``coverage``  - plain share of those terms covered by the passage.

    Query terms that appear nowhere in the corpus are excluded from both
    denominators: they can never be matched, so counting them only diluted the
    score of every passage and caused spurious abstentions.
    """
    query_terms = set(tokenize(query))
    if not query_terms:
        return []

    all_passages = [
        (document, passage, set(tokenize(passage)))
        for document in documents
        for passage in split_passages(document["body"])
    ]
    if not all_passages:
        return []

    total_passages = len(all_passages)
    document_frequency = {
        term: sum(term in terms for _, _, terms in all_passages)
        for term in query_terms
    }
    answerable_terms = {term for term in query_terms if document_frequency[term] > 0}
    if not answerable_terms:
        return []

    def weight(term: str) -> float:
        return math.log(1 + total_passages / (1 + document_frequency[term]))

    maximum_weight = sum(weight(term) for term in answerable_terms)
    query_term_count = len(query_terms)

    candidates: list[dict[str, Any]] = []
    for document, passage, terms in all_passages:
        overlap = answerable_terms & terms
        if not overlap:
            continue
        weighted_matches = sum(weight(term) for term in overlap)
        score = weighted_matches / maximum_weight if maximum_weight else 0.0
        candidates.append({
            "document_id": document["id"],
            "document_name": document["name"],
            "source": document["source"],
            "passage": passage,
            "score": round(score, 3),
            "coverage": round(len(overlap) / query_term_count, 3),
            "matched_terms": len(overlap),
        })
    candidates.sort(key=lambda item: (item["score"], item["matched_terms"]), reverse=True)
    return candidates[:3]


def _build_citations(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn ranked retrieval matches into numbered, citable references."""
    citations: list[dict[str, Any]] = []
    for match in matches or []:
        weak_score = match["score"] < MIN_RETRIEVAL_SCORE
        weak_coverage = match.get("coverage", 0.0) < MIN_EVIDENCE_COVERAGE
        if weak_score and weak_coverage:
            continue
        citations.append({
            "reference_id": f"S{len(citations) + 1}",
            **{
                key: match[key]
                for key in ("document_id", "document_name", "source", "passage", "score")
            },
            "coverage": match.get("coverage", 0.0),
        })
    return citations


def _build_user_content(
    query: str, source_context: str, image: str | None, audio: str | None
) -> Any:
    if image or audio:
        text = (
            f"The user attached an image/audio clip for you to analyze directly. "
            f"Question: {query}\n\n"
            f"Retrieved source excerpts (cite with [S1] etc. when used):\n{source_context}"
        )
    else:
        text = f"Question: {query}\n\nRetrieved source excerpts:\n{source_context}"
    if not image and not audio:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if image:
        parts.append({"type": "image_url", "image_url": {"url": image}})
    if audio:
        if audio.startswith("data:"):
            parts.append({"type": "audio_url", "audio_url": {"url": audio}})
        else:
            parts.append({"type": "audio_url", "audio_url": {"url": audio}})
    return parts


REFERENCE_TOKEN = re.compile(r"\[(S\d+)\]")


def _finalize_answer(
    answer: str, citations: list[dict[str, Any]], valid_reference_ids: set[str]
) -> str:
    """Keep the model answer tied to the passages that were actually retrieved.

    The model is asked to cite sources as ``[S1]``, but it can still invent a
    reference, cite a passage it was not given, or drop the citations entirely.
    Any token that does not match a retrieved passage is removed so the answer
    can never point at a source that was not sent to the model.
    """
    if not valid_reference_ids:
        return answer

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        return match.group(0) if token in valid_reference_ids else ""

    cleaned = REFERENCE_TOKEN.sub(replace, answer)
    # Collapse whitespace left behind by removed tokens.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    if not REFERENCE_TOKEN.search(cleaned):
        sources = ", ".join(
            f"[{citation['reference_id']}] {citation['document_name']}"
            for citation in citations
        )
        cleaned = f"{cleaned}\\n\\nSources: {sources}"
    return cleaned


async def generate_answer(
    query: str,
    citations: list[dict[str, Any]],
    image: str | None = None,
    audio: str | None = None,
    conversational: bool = False,
) -> tuple[str, str]:
    if not LLM_API_KEY:
        if conversational:
            return CONVERSATIONAL_REPLY, "conversational-extractive"
        return "Retrieved source excerpts are shown below. This system does not generate or validate work instructions.", "extractive"
    fallback = "The model service was unavailable or returned no answer. Source excerpts are shown below; please retry."

    source_context = "\n\n".join(
        f"[{citation['reference_id']}] {citation['document_name']} ({citation['source']}):\n{citation['passage']}"
        for citation in citations
    ) or "No retrieved source excerpts."
    valid_reference_ids = {citation["reference_id"] for citation in citations}
    if conversational:
        system_content = (
            "You are a helpful industrial evidence assistant. "
            "Answer greetings and meta questions briefly and warmly. "
            "For domain questions, answer only using the supplied source excerpts and cite every "
            "factual statement with the exact reference token, such as [S1]. "
            "Never provide equipment operation, repair, maintenance, isolation, adjustment, "
            "troubleshooting, bypass, or work-step instructions. "
            "If no sources are available, say so and suggest relevant knowledge-base topics. "
            "The source may be synthetic and must never be described as an approved plant SOP."
        )
    else:
        system_content = (
            "Answer informational questions thoroughly using all of the supplied source excerpts. "
            "This is an industrial safety domain: never provide equipment operation, "
            "repair, maintenance, isolation, adjustment, troubleshooting, bypass, "
            "or work-step instructions. Do not convert source text into a procedure. "
            "Do not infer missing facts. If sources do not support an answer, say so. "
            "Cite every factual statement with the exact reference token, such as [S1], "
            "and use every relevant excerpt. "
            "You may also receive an image or audio clip; describe or transcribe what you "
            "observe and relate it to the retrieved sources when relevant. "
            "The source may be synthetic and must never be described as an approved plant SOP."
        )
    payload = {
        "model": LLM_MODEL,
        "temperature": 0,
        "max_tokens": 2048,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": _build_user_content(query, source_context, image, audio)},
        ],
    }

    last_exc = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{LLM_API_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json=payload,
                )
                response.raise_for_status()
            data = response.json()
            choices = data.get("choices") or []
            message = choices[0].get("message", {}) if choices else {}
            answer = ""
            if isinstance(message, dict):
                answer = message.get("content")
                if not answer:
                    answer = message.get("reasoning_content")
                if not answer and isinstance(message.get("content"), list):
                    parts = []
                    for part in message["content"]:
                        if isinstance(part, dict):
                            parts.append(part.get("text", ""))
                        else:
                            parts.append(str(part))
                    answer = "".join(parts)
            if not answer and choices:
                answer = choices[0].get("text", "")
            answer = (answer or "").strip()
            answer = re.sub(r"<unk>", "", answer)
            answer = re.sub(r"<\|[^|]*\|>", "", answer)
            answer = re.sub(r"[ \t]{2,}", " ", answer)
            answer = re.sub(r"\n{3,}", "\n\n", answer).strip()
            if answer:
                mode = "conversational-llm" if conversational else "grounded-llm"
                return _finalize_answer(answer, citations, valid_reference_ids), mode
        except Exception as exc:
            last_exc = exc
            import sys as _e
            print("LLM call failed (attempt %d):" % (attempt + 1), exc, file=_e.stderr)
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
    if conversational:
        return CONVERSATIONAL_REPLY, "conversational-extractive"
    return fallback, "extractive-fallback"



def log_query(
    query: str,
    grounded: bool,
    document_id: int | None,
    score: float,
    mode: str = "grounded",
) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO queries (query, grounded, document_id, retrieval_score, created_at, mode) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (query, int(grounded), document_id, score, now(), mode),
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="NexusSOP Evidence API",
    version="1.0.0",
    description="Local-first, evidence-only SOP retrieval demo with review-gated ingestion.",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(ROOT / "industrial_genai_sop_assistant.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    # A tiny transparent ICO so the browser stops requesting a missing file.
    icon = (
        b"\x00\x00\x01\x00\x01\x00\x10\x10\x00\x00\x01\x00\x20\x00\x68\x04"
        b"\x00\x00\x16\x00\x00\x00"
    )
    payload = icon + b"\x00" * (4096 - len(icon))
    return Response(payload, media_type="image/x-icon")


@app.get("/api/uploads/{image_name}", include_in_schema=False)
def serve_uploaded_image(image_name: str) -> FileResponse:
    """Serve an uploaded image file for inline display."""
    image_path = IMAGE_UPLOAD_DIR / image_name
    if not image_path.exists():
        raise HTTPException(404, "Image not found.")
    return FileResponse(image_path)


@app.get("/live", include_in_schema=False)
@app.get("/live-audio", include_in_schema=False)
@app.get("/live_audio_chat.html", include_in_schema=False)
@app.get("/voice-chat", include_in_schema=False)
def live_audio_chat() -> FileResponse:
    return FileResponse(ROOT / "live_audio_chat.html")

@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "evidence-only", "model": "extractive retrieval"}


@app.get("/api/voice/config")
def voice_config() -> dict[str, str]:
    return {
        "model": GEMINI_LIVE_MODEL,
        "ws_url": "/api/voice/ws",
    }


def build_voice_knowledge(query: str) -> dict[str, Any]:
    """Search approved extracted document text for a Live API tool call."""
    with connect() as connection:
        documents = connection.execute(
            "SELECT id, name, source, approved, created_at, length(body) AS characters "
            "FROM documents WHERE approved = 1 ORDER BY id DESC"
        ).fetchall()
        rows = connection.execute(
            "SELECT * FROM documents WHERE approved = 1"
        ).fetchall()

    catalog = [dict(document) for document in documents]
    citations = _build_citations(retrieve(query, rows))
    catalog_question = bool(
        re.search(r"\b(document|documents|uploaded|upload|knowledge vault|sources?)\b", query, re.I)
    )
    if catalog_question:
        names = ", ".join(document["name"] for document in catalog)
        answer = (
            f"Approved documents: {names}."
            if names
            else "There are no approved documents in the knowledge vault."
        )
    elif citations:
        answer = "Relevant approved document excerpts were found."
    elif catalog:
        names = ", ".join(document["name"] for document in catalog)
        answer = f"No strong passage matched the question. Approved documents: {names}."
    else:
        answer = "There are no approved documents in the knowledge vault."

    extracted_sources = [
        {
            "reference_id": citation["reference_id"],
            "document_name": citation["document_name"],
            "source": citation["source"],
            "extracted_text": citation["passage"][:1400],
            "score": citation["score"],
        }
        for citation in citations
    ]
    return {
        "answer": answer,
        "abstained": not bool(citations) and not catalog_question,
        "approved_documents": [
            {
                "name": document["name"],
                "source": document["source"],
                "characters": document["characters"],
            }
            for document in catalog
        ],
        "extracted_sources": extracted_sources,
    }


async def handle_live_tool_call(upstream: Any, tool_call: dict[str, Any]) -> list[dict[str, Any]]:
    """Execute supported Live tools on the server and answer Gemini directly."""
    function_responses = []
    display_results = []
    for call in tool_call.get("functionCalls", []):
        if call.get("name") != "search_knowledge_base":
            function_responses.append({
                "name": call.get("name", ""),
                "id": call.get("id", ""),
                "response": {"result": "Unknown tool."},
            })
            continue
        query = str(call.get("args", {}).get("query", "")).strip()
        if not query:
            result = {"error": "A non-empty query is required."}
        else:
            result = await asyncio.to_thread(build_voice_knowledge, query)
        display_results.append(result)
        function_responses.append({
            "name": call["name"],
            "id": call["id"],
            "response": {"result": json.dumps(result, ensure_ascii=True)},
        })
    await upstream.send(json.dumps({"toolResponse": {"functionResponses": function_responses}}))
    return display_results


@app.post("/api/sessions", status_code=201)
def start_session(request: SessionRequest) -> dict[str, str]:
    return {"session_id": create_session(request.kind), "kind": request.kind}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    with connect() as connection:
        session = connection.execute(
            "SELECT id, kind, created_at, updated_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not session:
            raise HTTPException(404, "Session not found.")
        messages = connection.execute(
            "SELECT id, role, content, modality, created_at FROM session_messages "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    return {"session": dict(session), "messages": [dict(message) for message in messages]}


@app.websocket("/api/voice/ws")
async def voice_websocket(websocket: WebSocket) -> None:
    """Relay a browser Live API session without exposing the Gemini API key."""
    await websocket.accept()
    session_id = ensure_session(websocket.query_params.get("session_id"), "voice")
    await websocket.send_text(json.dumps({"sessionInfo": {"session_id": session_id}}))
    if not GEMINI_API_KEY:
        await websocket.close(code=1011, reason="Gemini Live API is not configured.")
        return

    upstream_url = (
        "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
        f"?key={GEMINI_API_KEY}"
    )
    try:
        async with websockets.connect(upstream_url, open_timeout=20, close_timeout=5) as upstream:
            async def browser_to_gemini() -> None:
                while True:
                    message = await websocket.receive_text()
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError:
                        payload = {}
                    realtime_input = payload.get("realtimeInput", {})
                    if realtime_input.get("text"):
                        log_session_message(
                            session_id, "user", realtime_input["text"], "voice"
                        )
                    await upstream.send(message)

            async def gemini_to_browser() -> None:
                async for message in upstream:
                    if isinstance(message, bytes):
                        message = message.decode("utf-8")
                    try:
                        payload = json.loads(message)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        await websocket.send_text(message)
                        continue
                    if payload.get("toolCall"):
                        results = await handle_live_tool_call(upstream, payload["toolCall"])
                        for result in results:
                            log_session_message(
                                session_id,
                                "tool",
                                json.dumps(result, ensure_ascii=True),
                                "tool",
                            )
                        await websocket.send_text(json.dumps({
                            "nexusToolResult": results,
                        }))
                        continue
                    server_content = payload.get("serverContent", {})
                    input_transcription = server_content.get("inputTranscription", {})
                    output_transcription = server_content.get("outputTranscription", {})
                    if input_transcription.get("text"):
                        log_session_message(
                            session_id, "user", input_transcription["text"], "voice"
                        )
                    if output_transcription.get("text"):
                        log_session_message(
                            session_id, "assistant", output_transcription["text"], "voice"
                        )
                    await websocket.send_text(message)

            tasks = [
                asyncio.create_task(browser_to_gemini()),
                asyncio.create_task(gemini_to_browser()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, return_exceptions=True)
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        return
    except Exception:
        if websocket.client_state.name == "CONNECTED":
            await websocket.close(code=1011, reason="Gemini Live connection failed.")


@app.get("/api/documents")
def list_documents() -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, name, source, image_path, approved, created_at, length(body) AS characters "
            "FROM documents ORDER BY id DESC"
        ).fetchall()
    return {"documents": [dict(row) for row in rows]}


@app.post("/api/voice/knowledge")
def voice_knowledge(request: VoiceKnowledgeRequest) -> dict[str, Any]:
    """Return approved document metadata and grounded passages for Live tool calls."""
    result = build_voice_knowledge(request.query.strip())
    result["documents"] = result.pop("approved_documents")
    result["citations"] = [
        {
            "reference_id": source["reference_id"],
            "document_name": source["document_name"],
            "source": source["source"],
            "passage": source["extracted_text"],
            "score": source["score"],
        }
        for source in result.pop("extracted_sources")
    ]
    return result


@app.post("/api/documents", status_code=201)
async def upload_document(file: UploadFile = File(...)) -> dict[str, Any]:
    name = Path(file.filename or "").name
    extension = Path(name).suffix.lower()
    if extension not in DOCUMENT_EXTENSIONS:
        raise HTTPException(415, "Upload a PDF, TXT, Markdown, or image file (PNG, JPG, WEBP, BMP, TIFF).")
    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "The maximum upload size is 10 MB.")
    image_path: Path | None = None
    try:
        if extension == ".pdf":
            reader = PdfReader(io.BytesIO(payload))
            body = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        elif extension in IMAGE_EXTENSIONS:
            image_path, body = prepare_image_for_storage(payload, name)
        else:
            body = payload.decode("utf-8-sig")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "Could not read this document. Check that it is a valid file.") from exc
    if len(body.strip()) < 20:
        raise HTTPException(422, "No searchable text was found in this file.")

    with connect() as connection:
        cursor = connection.execute(
            "INSERT INTO documents (name, body, source, image_path, approved, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (name, body, "Uploaded; review required", str(image_path) if image_path else None, now()),
        )
        document_id = cursor.lastrowid
    return {
        "id": document_id,
        "name": name,
        "approved": False,
        "status": "Pending human review",
        "characters": len(body),
        "image_path": str(image_path) if image_path else None,
    }


@app.post("/api/documents/{document_id}/approve")
def approve_document(document_id: int) -> dict[str, Any]:
    with connect() as connection:
        result = connection.execute(
            "UPDATE documents SET approved = 1, source = 'Human-reviewed upload' WHERE id = ?",
            (document_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Document not found.")
        row = connection.execute(
            "SELECT id, name, source, approved FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    return dict(row)


@app.post("/api/chat")
async def chat(request: ChatRequest) -> dict[str, Any]:
    query = request.query.strip()
    session_id = ensure_session(request.session_id, "text")
    log_session_message(session_id, "user", query, "text")
    if is_operational_request(query):
        response = {
            "grounded": False,
            "abstained": True,
            "reason": "Operational or safety-critical intent requires an approved site procedure and qualified human review.",
            "answer": "I can't provide equipment operating, repair, isolation, or troubleshooting instructions. Stop and consult the current approved site SOP and a qualified supervisor.",
            "citations": [],
            "retrieval_score": 0.0,
        }
        log_query(query, False, None, 0.0, mode="refusal")
        log_session_message(session_id, "assistant", response["answer"], "text")
        response["session_id"] = session_id
        return response

    # Greetings / meta questions: still run retrieval (RAG) first, but answer
    # conversationally when no strong evidence exists.
    is_greeting = bool(GREETING_PATTERN.search(query))

    with connect() as connection:
        if request.document_id is None:
            rows = connection.execute("SELECT * FROM documents WHERE approved = 1").fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM documents WHERE approved = 1 AND id = ?", (request.document_id,)
            ).fetchall()
    matches = retrieve(query, rows)
    citations = _build_citations(matches)

    if is_greeting:
        answer, generation_mode = await generate_answer(
            query, citations, image=request.image, audio=request.audio, conversational=True
        )
        top_score = citations[0]["score"] if citations else 0.0
        top_doc = citations[0]["document_id"] if citations else None
        log_query(query, True, top_doc, top_score, mode="conversational")
        response = {
            "grounded": True,
            "abstained": False,
            "reason": None,
            "answer": answer,
            "generation_mode": generation_mode,
            "citations": citations,
            "retrieval_score": top_score,
        }
        log_session_message(session_id, "assistant", answer, "text")
        response["session_id"] = session_id
        return response

    has_media = bool(request.image or request.audio)
    # Evidence gate. The relevance score alone is not enough: an IDF-weighted
    # score can be high while only one of many query terms is actually
    # addressed. Coverage (share of query terms the passage answers) is the
    # deciding signal, so both must clear their thresholds.
    low_evidence = (
        not citations
        or citations[0]["coverage"] < MIN_EVIDENCE_COVERAGE
        or citations[0]["score"] < MIN_RETRIEVAL_SCORE
    )
    if low_evidence and not has_media:
        log_query(
            query, False, None, citations[0]["score"] if citations else 0.0, mode="abstained"
        )
        response = {
            "grounded": False,
            "abstained": True,
            "reason": "No sufficiently relevant passage was found in the approved documents.",
            "answer": "I couldn't find enough evidence in the approved knowledge base to answer that. Upload a relevant source, have it reviewed, then ask again.",
            "citations": [],
            "retrieval_score": citations[0]["score"] if citations else 0.0,
        }
        log_session_message(session_id, "assistant", response["answer"], "text")
        response["session_id"] = session_id
        return response

    top_score = citations[0]["score"] if citations else 0.0
    top_doc = citations[0]["document_id"] if citations else None
    answer, generation_mode = await generate_answer(
        query, citations, image=request.image, audio=request.audio
    )
    log_query(query, True, top_doc, top_score)
    response = {
        "grounded": True,
        "abstained": False,
        "reason": None,
        "answer": answer,
        "generation_mode": generation_mode,
        "citations": citations,
        "retrieval_score": top_score,
    }
    log_session_message(session_id, "assistant", answer, "text")
    response["session_id"] = session_id
    return response


@app.get("/api/analytics")
def analytics() -> dict[str, Any]:
    with connect() as connection:
        query_stats = connection.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN mode = 'grounded' THEN 1 ELSE 0 END) AS grounded, "
            "SUM(CASE WHEN mode IN ('abstained', 'refusal') THEN 1 ELSE 0 END) AS abstained, "
            "SUM(CASE WHEN mode = 'conversational' THEN 1 ELSE 0 END) AS conversational, "
            "AVG(CASE WHEN mode = 'grounded' THEN retrieval_score END) AS average_score "
            "FROM queries"
        ).fetchone()
        document_stats = connection.execute(
            "SELECT COUNT(*) AS total, SUM(approved) AS approved FROM documents"
        ).fetchone()
        escalation_count = connection.execute("SELECT COUNT(*) FROM escalations").fetchone()[0]
        latest = connection.execute(
            "SELECT query, grounded, retrieval_score, created_at, mode FROM queries "
            "ORDER BY id DESC LIMIT 10"
        ).fetchall()
    total = query_stats["total"] or 0
    grounded = query_stats["grounded"] or 0
    abstained = query_stats["abstained"] or 0
    return {
        "queries": total,
        "grounded": grounded,
        "abstained": abstained,
        "conversational": query_stats["conversational"] or 0,
        "documents": document_stats["total"] or 0,
        "approved_documents": document_stats["approved"] or 0,
        "escalations": escalation_count,
        "average_retrieval_score": round(query_stats["average_score"] or 0, 3),
        "recent_queries": [dict(row) for row in latest],
    }


@app.post("/api/escalations", status_code=201)
def create_escalation(request: EscalationRequest) -> dict[str, Any]:
    with connect() as connection:
        cursor = connection.execute(
            "INSERT INTO escalations (query, reason, created_at) VALUES (?, ?, ?)",
            (request.query.strip(), request.reason.strip(), now()),
        )
        ticket_id = cursor.lastrowid
    return {"id": ticket_id, "status": "open", "created_at": now()}


def neutralize_formula(query: str) -> str:
    """Prefix spreadsheet formulas so the CSV export cannot execute them."""
    if query.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + query
    return query


@app.get("/api/analytics/export")
def export_analytics() -> StreamingResponse:
    with connect() as connection:
        rows = connection.execute(
            "SELECT query, grounded, retrieval_score, created_at, mode "
            "FROM queries ORDER BY id DESC"
        ).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["query", "grounded", "retrieval_score", "mode", "created_at"])
    writer.writerows(
        (neutralize_formula(row["query"]),
         row["grounded"], row["retrieval_score"], row["mode"], row["created_at"])
        for row in rows
    )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=nexus-sop-analytics.csv"},
    )