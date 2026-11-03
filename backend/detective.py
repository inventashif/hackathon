"""Manual-grounded Smart Fault Detective.

Technical follow-up questions are generated only from approved manuals for the
selected machine/model. Pending, rejected, or unapproved documents are never
used. Dangerous electrical or mechanical inspection is never requested.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from pydantic import BaseModel, Field


INSUFFICIENT_MESSAGE = (
    "The approved manual does not provide sufficient information for this "
    "diagnostic question. Please consult an authorized technician."
)

SAFETY_ESCALATION_MESSAGE = (
    "The remaining checks in the approved manual require physical or electrical "
    "inspection. Do not open covers, measure live circuits, or disassemble "
    "equipment. Please consult an authorized technician."
)

GENERAL_QUESTIONS = [
    {
        "id": "general_what_happened",
        "text": "What happened?",
        "kind": "general",
    },
    {
        "id": "general_when_started",
        "text": "When did the problem start?",
        "kind": "general",
    },
    {
        "id": "general_visible_warning",
        "text": "Is there any visible warning, alarm, or display message?",
        "kind": "general",
    },
]

UNSAFE_PATTERN = re.compile(
    r"\b("
    r"voltage|volt|amper|current|energiz|live\s+wire|arc\s*flash|shock|"
    r"lock-?out|tag-?out|\bloto\b|disconnect|isolate\s+power|"
    r"remove\s+(the\s+)?(cover|panel|guard|housing)|"
    r"open\s+(the\s+)?(cabinet|enclosure|panel|door)|"
    r"measure|multimeter|probe|megger|"
    r"disassembl|replace\s+(the\s+)?|tighten|torque|"
    r"bleed|pressuri|hydraulic\s+pressure|compressed\s+air|"
    r"confined\s+space|climb|hot\s+surface"
    r")\b",
    re.IGNORECASE,
)

CAUSE_HEADING = re.compile(
    r"(possible\s+causes?|causes?|recommended\s+checks?|troubleshooting|"
    r"fault\s+description|corrective\s+action|remed[yi])",
    re.IGNORECASE,
)
CHECK_LEAD = re.compile(
    r"^\s*(?:[-*•]|\d+[.)])\s*(?:check\s+(?:if|whether)|verify\s+that|"
    r"confirm\s+that|ensure\s+that|observe|look\s+for|if\s+the)\b",
    re.IGNORECASE,
)
OBSERVABLE_INDICATOR = re.compile(
    r"\b(display|panel|bop|hmi|screen|lamp|led|indicator|warning|alarm|"
    r"code|message|sound|noise|smell|odor|smoke|leak|leakage|drip|fluid|"
    r"vibration|shak|speed|rotat|deceleration|acceleration|"
    r"stopping|starting|trip|tripped|run|running|idle|stall|stalled|"
    r"visible|observe|sight|external)\b",
    re.IGNORECASE,
)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?;:])\s+|\n+")
SECTION_HEADING = re.compile(r"^(#{1,6}\s+.+|[A-Z][A-Za-z0-9][^\n]{2,80})$")
MAX_TECHNICAL_QUESTIONS = 5
MIN_QUESTION_OVERLAP = 2


class DetectiveStartRequest(BaseModel):
    machine_name: str = Field(min_length=1, max_length=200)
    machine_model: str = Field(default="", max_length=200)
    error_code: str = Field(default="", max_length=80)
    document_id: int | None = None
    initial_symptom: str = Field(default="", max_length=2000)


class DetectiveAnswerRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


def init_detective_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS detective_sessions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            machine_name TEXT NOT NULL,
            machine_model TEXT,
            error_code TEXT,
            document_id INTEGER,
            status TEXT NOT NULL,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS detective_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('assistant', 'user')),
            question_id TEXT,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            source_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES detective_sessions(id) ON DELETE CASCADE
        )
        """
    )


def _new_state() -> dict[str, Any]:
    return {
        "asked_ids": [],
        "answers": {},
        "used_excerpts": [],
        "general_index": 0,
        "technical_count": 0,
        "phase": "collecting",
    }


def _load_session(connection: sqlite3.Connection, session_id: str, user_id: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM detective_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_id),
    ).fetchone()
    if not row:
        from fastapi import HTTPException

        raise HTTPException(404, "Detective session not found.")
    return row


def _state(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["state_json"] or "{}")


def _save_state(
    connection: sqlite3.Connection,
    session_id: str,
    status: str,
    state: dict[str, Any],
) -> None:
    from backend.main import now

    connection.execute(
        "UPDATE detective_sessions SET status = ?, state_json = ?, updated_at = ? WHERE id = ?",
        (status, json.dumps(state), now(), session_id),
    )


def _add_turn(
    connection: sqlite3.Connection,
    session_id: str,
    role: str,
    kind: str,
    content: str,
    question_id: str | None = None,
    source: dict[str, Any] | None = None,
) -> None:
    from backend.main import now

    connection.execute(
        """
        INSERT INTO detective_turns
            (session_id, role, question_id, kind, content, source_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            role,
            question_id,
            kind,
            content,
            json.dumps(source) if source else None,
            now(),
        ),
    )


def _session_payload(connection: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM detective_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    turns = connection.execute(
        "SELECT role, question_id, kind, content, source_json, created_at "
        "FROM detective_turns WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    ).fetchall()
    manuals = _approved_manuals_for_machine(
        connection,
        row["machine_name"],
        row["machine_model"] or "",
        row["document_id"],
    )
    current = None
    for turn in reversed(turns):
        if turn["role"] == "assistant" and turn["kind"] in {
            "general",
            "technical",
            "insufficient",
            "safety_escalation",
        }:
            current = {
                "question_id": turn["question_id"],
                "kind": turn["kind"],
                "content": turn["content"],
                "source": json.loads(turn["source_json"]) if turn["source_json"] else None,
            }
            break
    return {
        "id": row["id"],
        "status": row["status"],
        "machine_name": row["machine_name"],
        "machine_model": row["machine_model"] or "",
        "error_code": row["error_code"] or "",
        "document_id": row["document_id"],
        "approved_manuals": [
            {"id": doc["id"], "name": doc["name"]} for doc in manuals
        ],
        "turns": [
            {
                "role": turn["role"],
                "question_id": turn["question_id"],
                "kind": turn["kind"],
                "content": turn["content"],
                "source": json.loads(turn["source_json"]) if turn["source_json"] else None,
                "created_at": turn["created_at"],
            }
            for turn in turns
        ],
        "current_question": current,
        "awaiting_answer": row["status"] in {"collecting", "questioning"},
        "insufficient_message": INSUFFICIENT_MESSAGE,
    }


def _approved_manuals_for_machine(
    connection: sqlite3.Connection,
    machine_name: str,
    machine_model: str,
    document_id: int | None,
) -> list[sqlite3.Row]:
    if document_id is not None:
        rows = connection.execute(
            "SELECT * FROM documents WHERE id = ? AND approved = 1 AND rejected = 0",
            (document_id,),
        ).fetchall()
        return list(rows)

    model_clean = machine_model.strip()
    name_clean = machine_name.strip()

    if model_clean and name_clean:
        rows = connection.execute(
            "SELECT * FROM documents WHERE approved = 1 AND rejected = 0 "
            "AND (name LIKE ? OR (name LIKE ? AND name LIKE ?))",
            (f"%{model_clean}%", f"%{name_clean}%", f"%{model_clean}%"),
        ).fetchall()
        if rows:
            return list(rows)

    if model_clean:
        rows = connection.execute(
            "SELECT * FROM documents WHERE approved = 1 AND rejected = 0 AND (name LIKE ? OR body LIKE ?)",
            (f"%{model_clean}%", f"%{model_clean}%"),
        ).fetchall()
        if rows:
            return list(rows)

    if name_clean:
        name_tokens = [
            t for t in re.split(r"[\s,/_\-]+", name_clean)
            if len(t) >= 3 and t.lower() not in {"machine", "equipment", "system", "unit", "device"}
        ]
        if name_tokens:
            clauses = ["name LIKE ?"] * len(name_tokens)
            params = [f"%{t}%" for t in name_tokens]
            rows = connection.execute(
                f"SELECT * FROM documents WHERE approved = 1 AND rejected = 0 AND ({' AND '.join(clauses)})",
                params,
            ).fetchall()
            if rows:
                return list(rows)

            clauses = ["(name LIKE ? OR body LIKE ?)"] * len(name_tokens)
            params = []
            for t in name_tokens:
                params.extend([f"%{t}%", f"%{t}%"])
            rows = connection.execute(
                f"SELECT * FROM documents WHERE approved = 1 AND rejected = 0 AND ({' AND '.join(clauses)})",
                params,
            ).fetchall()
            if rows:
                return list(rows)

    return []


def _page_and_section(passage: str) -> tuple[int | None, str]:
    from backend.main import PAGE_MARKER_REGEX

    page = None
    match = PAGE_MARKER_REGEX.search(passage)
    if match:
        page = int(match.group(1))
    section = ""
    for line in passage.splitlines():
        stripped = line.strip()
        if stripped.startswith("[[page:"):
            continue
        if stripped.startswith("#"):
            section = re.sub(r"^#+\s*", "", stripped)
            break
        if SECTION_HEADING.match(stripped) and len(stripped) < 90:
            section = stripped
            break
    if not section:
        first = next(
            (line.strip() for line in passage.splitlines() if line.strip() and "[[page:" not in line),
            "",
        )
        section = first[:80]
    return page, section


def _source_from_match(match: dict[str, Any]) -> dict[str, Any]:
    page, section = _page_and_section(match["passage"])
    excerpt = re.sub(r"\[\[page:\d+\]\]", "", match["passage"]).strip()
    excerpt = re.sub(r"\s+", " ", excerpt)
    return {
        "document_id": match["document_id"],
        "document_name": match["document_name"],
        "page": page,
        "section": section,
        "excerpt": excerpt[:400],
    }


def _is_unsafe(text: str) -> bool:
    return bool(UNSAFE_PATTERN.search(text or ""))


def _question_is_grounded(question: str, passage: str) -> bool:
    from backend.main import tokenize

    q_terms = set(tokenize(question))
    p_terms = set(tokenize(passage))
    return len(q_terms & p_terms) >= MIN_QUESTION_OVERLAP


def _to_observational_question(sentence: str) -> str | None:
    cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", sentence).strip()
    cleaned = re.sub(r"\[\[page:\d+\]\]", "", cleaned).strip()
    if len(cleaned) < 12 or _is_unsafe(cleaned):
        return None
    lowered = cleaned.lower()
    if lowered.startswith(("check whether", "check if", "verify that", "confirm that", "ensure that")):
        rest = re.sub(
            r"^(check whether|check if|verify that|confirm that|ensure that)\s+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).rstrip(".")
        return (
            f"From the operator station, can you observe whether {rest}, "
            "as described in the approved manual?"
        )
    if lowered.startswith("look for") or lowered.startswith("observe"):
        rest = cleaned.rstrip(".")
        return f"Can you see {rest[rest.find(' ') + 1:]} from a safe operator position?"
    if CAUSE_HEADING.search(cleaned) or "fault" in lowered or "alarm" in lowered:
        if OBSERVABLE_INDICATOR.search(lowered):
            snippet = cleaned.rstrip(".")
            if len(snippet) > 220:
                snippet = snippet[:217] + "..."
            return (
                "The approved manual describes this condition: "
                f"“{snippet}”. Does what you see on the machine match this description?"
            )
        return None
    return None


def _extract_candidates(match: dict[str, Any]) -> list[dict[str, Any]]:
    passage = match["passage"]
    source = _source_from_match(match)
    candidates: list[dict[str, Any]] = []
    lines = [line.strip() for line in passage.splitlines() if line.strip()]
    chunks = lines[:]
    chunks.extend(SENTENCE_SPLIT.split(passage))
    seen: set[str] = set()
    for chunk in chunks:
        if _is_unsafe(chunk):
            continue
        question = None
        if CHECK_LEAD.search(chunk) or CAUSE_HEADING.search(chunk):
            question = _to_observational_question(chunk)
        elif any(
            token in chunk.lower()
            for token in ("fault", "alarm", "display", "warning", "symptom", "cause")
        ):
            question = _to_observational_question(chunk)
        if not question:
            continue
        key = question.lower()
        if key in seen:
            continue
        if not _question_is_grounded(question, passage):
            continue
        seen.add(key)
        candidates.append({"text": question, "source": source, "passage": passage})
    return candidates


def _retrieve_manual_matches(
    connection: sqlite3.Connection,
    machine_name: str,
    machine_model: str,
    error_code: str,
    symptoms: str,
    document_id: int | None,
) -> list[dict[str, Any]]:
    from backend.main import retrieve

    manuals = _approved_manuals_for_machine(
        connection, machine_name, machine_model, document_id
    )
    if not manuals:
        return []
    query_parts = [machine_name, machine_model, error_code, symptoms]
    query = " ".join(part for part in query_parts if part).strip()
    if not query:
        return []
    return retrieve(query, manuals, limit=8)


def _next_technical_question(state: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    used = set(state.get("used_excerpts") or [])
    asked = set(state.get("asked_ids") or [])
    for match in matches:
        excerpt_key = f"{match['document_id']}:{match['passage'][:120]}"
        if excerpt_key in used:
            continue
        for candidate in _extract_candidates(match):
            qid = f"tech_{abs(hash(candidate['text'])) % 10_000_000}"
            if qid in asked or candidate["text"] in asked:
                continue
            used.add(excerpt_key)
            state["used_excerpts"] = list(used)
            return {
                "id": qid,
                "text": candidate["text"],
                "kind": "technical",
                "source": candidate["source"],
            }
    return None


def _symptom_blob(state: dict[str, Any], error_code: str) -> str:
    answers = state.get("answers") or {}
    parts = [str(value) for value in answers.values() if value]
    if error_code:
        parts.append(error_code)
    return " ".join(parts)


def start_session(user_id: int, request: DetectiveStartRequest) -> dict[str, Any]:
    from backend.main import connect, now

    session_id = str(uuid.uuid4())
    state = _new_state()
    if request.initial_symptom.strip():
        state["answers"]["initial_symptom"] = request.initial_symptom.strip()
    if request.error_code.strip():
        state["answers"]["error_code"] = request.error_code.strip()

    with connect() as connection:
        init_detective_tables(connection)
        manuals = _approved_manuals_for_machine(
            connection,
            request.machine_name.strip(),
            request.machine_model.strip(),
            request.document_id,
        )
        connection.execute(
            """
            INSERT INTO detective_sessions
                (id, user_id, machine_name, machine_model, error_code, document_id,
                 status, state_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                user_id,
                request.machine_name.strip(),
                request.machine_model.strip(),
                request.error_code.strip(),
                request.document_id,
                "collecting",
                json.dumps(state),
                now(),
                now(),
            ),
        )
        intro = (
            "Smart Fault Detective will collect a few safe symptom questions, then ask "
            "technical follow-ups only from approved manuals for this machine and model. "
            "Pending or rejected documents are not used."
        )
        _add_turn(connection, session_id, "assistant", "intro", intro)

        if not manuals:
            _add_turn(
                connection,
                session_id,
                "assistant",
                "insufficient",
                INSUFFICIENT_MESSAGE,
            )
            _save_state(connection, session_id, "insufficient", state)
            return _session_payload(connection, session_id)

        first = GENERAL_QUESTIONS[0]
        state["asked_ids"].append(first["id"])
        state["general_index"] = 1
        _add_turn(
            connection,
            session_id,
            "assistant",
            "general",
            first["text"],
            question_id=first["id"],
        )
        _save_state(connection, session_id, "collecting", state)
        return _session_payload(connection, session_id)


def _advance_after_answer(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    state: dict[str, Any],
) -> None:
    session_id = row["id"]
    if state.get("phase") == "collecting":
        next_index = int(state.get("general_index") or 0)
        if next_index < len(GENERAL_QUESTIONS):
            nxt = GENERAL_QUESTIONS[next_index]
            state["asked_ids"].append(nxt["id"])
            state["general_index"] = next_index + 1
            _add_turn(
                connection,
                session_id,
                "assistant",
                "general",
                nxt["text"],
                question_id=nxt["id"],
            )
            _save_state(connection, session_id, "collecting", state)
            return
        state["phase"] = "questioning"

    if int(state.get("technical_count") or 0) >= MAX_TECHNICAL_QUESTIONS:
        _add_turn(
            connection,
            session_id,
            "assistant",
            "complete",
            "Enough observational answers have been collected. Remaining physical "
            "checks in the approved manual should be performed by an authorized technician.",
        )
        _save_state(connection, session_id, "complete", state)
        return

    symptoms = _symptom_blob(state, row["error_code"] or "")
    matches = _retrieve_manual_matches(
        connection,
        row["machine_name"],
        row["machine_model"] or "",
        row["error_code"] or "",
        symptoms,
        row["document_id"],
    )
    if not matches:
        _add_turn(
            connection,
            session_id,
            "assistant",
            "insufficient",
            INSUFFICIENT_MESSAGE,
        )
        _save_state(connection, session_id, "insufficient", state)
        return

    question = _next_technical_question(state, matches)
    if not question:
        unsafe_only = any(_is_unsafe(match["passage"]) for match in matches)
        message = SAFETY_ESCALATION_MESSAGE if unsafe_only else INSUFFICIENT_MESSAGE
        kind = "safety_escalation" if unsafe_only else "insufficient"
        _add_turn(connection, session_id, "assistant", kind, message)
        _save_state(
            connection,
            session_id,
            "insufficient" if kind == "insufficient" else "complete",
            state,
        )
        return

    state["asked_ids"].append(question["id"])
    state["technical_count"] = int(state.get("technical_count") or 0) + 1
    _add_turn(
        connection,
        session_id,
        "assistant",
        "technical",
        question["text"],
        question_id=question["id"],
        source=question["source"],
    )
    _save_state(connection, session_id, "questioning", state)


def answer_session(user_id: int, session_id: str, request: DetectiveAnswerRequest) -> dict[str, Any]:
    from backend.main import connect

    with connect() as connection:
        init_detective_tables(connection)
        row = _load_session(connection, session_id, user_id)
        if row["status"] not in {"collecting", "questioning"}:
            return _session_payload(connection, session_id)
        state = _state(row)
        asked = state.get("asked_ids") or []
        current_id = asked[-1] if asked else "answer"
        state.setdefault("answers", {})[current_id] = request.answer.strip()
        last_assistant = connection.execute(
            "SELECT kind FROM detective_turns WHERE session_id = ? AND role = 'assistant' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        kind = last_assistant["kind"] if last_assistant else "general"
        _add_turn(
            connection,
            session_id,
            "user",
            kind,
            request.answer.strip(),
            question_id=current_id,
        )
        _advance_after_answer(connection, row, state)
        return _session_payload(connection, session_id)


def get_session(user_id: int, session_id: str) -> dict[str, Any]:
    from backend.main import connect

    with connect() as connection:
        init_detective_tables(connection)
        _load_session(connection, session_id, user_id)
        return _session_payload(connection, session_id)
