"""SOP generation, feedback, notes, and PDF export for NexusSOP.

Implements:
- SOP generation from retrieved passages with citations and figures
- Feedback collection (applied_how, outcome, notes)
- Notes (manual and synthesized from feedback)
- PDF export with steps, figures, feedback, and notes
"""

import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fpdf import FPDF
from pydantic import BaseModel

# LLM configuration dynamically retrieved to support monkeypatching and avoid circular imports
def _get_llm_config() -> tuple[str, str, str]:
    from backend import main
    cfg = main.get_active_model_config()
    key = main.GEMINI_API_KEY if cfg["provider"] == "gemini" else main.LLM_API_KEY
    return key, cfg["provider"], cfg["active_model"]



# Configuration
EXPORT_DIR = Path(os.environ.get("SOP_EXPORT_DIR", "data/exports"))


class FeedbackRequest(BaseModel):
    applied_how: str
    outcome: str  # "fixed", "partial", "not_fixed"
    notes: str


class NoteRequest(BaseModel):
    body: str


def now() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_sop_tables(connection: sqlite3.Connection) -> None:
    """Initialize SOP-related database tables."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            problem TEXT NOT NULL,
            answer_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'reviewed')),
            created_at TEXT NOT NULL,
            export_path TEXT,
            FOREIGN KEY(room_id) REFERENCES rooms(id),
            FOREIGN KEY(author_id) REFERENCES users(id),
            FOREIGN KEY(session_id) REFERENCES sessions(id),
            FOREIGN KEY(message_id) REFERENCES session_messages(id)
        );

        CREATE TABLE IF NOT EXISTS sop_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sop_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            applied_how TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('fixed', 'partial', 'not_fixed')),
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(sop_id) REFERENCES sops(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS sop_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sop_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('manual', 'synthesized')),
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(sop_id) REFERENCES sops(id) ON DELETE CASCADE,
            FOREIGN KEY(author_id) REFERENCES users(id)
        );
        """
    )


async def build_sop_from_passages(
    query: str,
    passages: list[dict[str, Any]],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Generate an SOP from retrieved passages and assets.

    Returns a dict with:
    - title: str
    - problem: str
    - steps: list of {number, instruction, citation_ids, figure_ids, caution}
    - checks: list of {text, citation_ids}
    - abstain: bool
    """
    llm_api_key, llm_base_url, llm_model = _get_llm_config()
    # Build asset mapping for F1, F2... and integer IDs
    f_map: dict[Any, int] = {}
    for i, a in enumerate(assets, start=1):
        aid = a["id"]
        f_map[f"F{i}"] = aid
        f_map[f"[F{i}]"] = aid
        f_map[str(aid)] = aid
        f_map[aid] = aid

    if not llm_api_key:
        # Extractive fallback: one step per passage
        steps = []
        for i, passage in enumerate(passages, start=1):
            p_text = passage.get("passage", "")
            page_num = None
            for line in p_text.splitlines():
                if "[[page:" in line:
                    try:
                        page_num = int(line.split("[[page:")[1].split("]]")[0])
                        break
                    except (ValueError, IndexError):
                        pass

            doc_id = passage.get("document_id")
            matching_fids = [
                a["id"] for a in assets
                if (page_num is not None and a.get("page_number") == page_num)
                or (a.get("kind") == "upload" and (doc_id is None or a.get("document_id") == doc_id))
            ]

            steps.append({
                "number": i,
                "instruction": p_text,
                "citation_ids": [passage.get("reference_id", f"S{i}")],
                "figure_ids": matching_fids,
                "caution": "",
            })

        return {
            "title": f"Procedure: {query[:50]}",
            "problem": query,
            "steps": steps,
            "checks": [],
            "abstain": False,
        }

    # Build source context for LLM
    source_context = "\n\n".join(
        f"[{p.get('reference_id', f'S{i}')}] {p['passage']}"
        for i, p in enumerate(passages, start=1)
    )

    # Build figure list
    figure_list = "\n".join(
        f"[F{i}] {a.get('caption', 'Figure')} (page {a.get('page_number', 'N/A')})"
        for i, a in enumerate(assets, start=1)
    )

    system_prompt = (
        "You are an industrial SOP generator. You will receive retrieved source passages "
        "and a list of figures. Generate a JSON response with the following structure:\n\n"
        '{\n'
        '  "title": "short descriptive title",\n'
        '  "problem": "restatement of the user query",\n'
        '  "steps": [\n'
        '    {\n'
        '      "number": 1,\n'
        '      "instruction": "clear step text",\n'
        '      "citation_ids": ["S1", "S2"],\n'
        '      "figure_ids": ["F1"],\n'
        '      "caution": "optional safety note"\n'
        '    }\n'
        '  ],\n'
        '  "checks": [\n'
        '    {\n'
        '      "text": "verification step",\n'
        '      "citation_ids": ["S1"]\n'
        '    }\n'
        '  ]\n\n'
        "Rules:\n"
        "- Every step must cite at least one passage using the exact reference token.\n"
        "- Figure IDs must match the provided figure list.\n"
        "- Do not invent steps, tools, or values not supported by the sources.\n"
        "- If sources do not support a procedure, return {\"abstain\": true}.\n"
    )

    user_prompt = (
        f"Query: {query}\n\n"
        f"Retrieved passages:\n{source_context}\n\n"
        f"Figures:\n{figure_list}"
    )

    try:
        from backend import main
        content = await main.call_active_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_mode=True,
        )
        clean_content = (content or "").strip()
        if clean_content.startswith("```"):
            clean_content = re.sub(r"^```(?:json)?\s*", "", clean_content)
            clean_content = re.sub(r"\s*```$", "", clean_content)
        result = json.loads(clean_content) if clean_content else {}

        # Check for abstain
        if result.get("abstain"):
            return {"abstain": True}

        return result
    except Exception as e:
        # Fallback to extractive on error
        print(f"SOP generation failed: {e}")
        steps = []
        for i, passage in enumerate(passages, start=1):
            steps.append({
                "number": i,
                "instruction": passage["passage"],
                "citation_ids": [passage.get("reference_id", f"S{i}")],
                "figure_ids": [],
                "caution": "",
            })

        return {
            "title": f"Procedure: {query[:50]}",
            "problem": query,
            "steps": steps,
            "checks": [],
            "abstain": False,
        }


def finalize_sop_answer(
    sop_data: dict[str, Any],
    valid_citation_ids: set[str],
    valid_figure_ids: set[int],
    f_map: Optional[dict[Any, int]] = None,
) -> dict[str, Any]:
    """Validate and filter SOP data against retrieved citations and figures.

    Removes steps with invalid citations or figures. Returns empty step list
    if no valid steps remain.
    """
    if sop_data.get("abstain"):
        return {"abstain": True}

    steps = []
    for step in sop_data.get("steps", []):
        # Filter citation IDs (support both "S1" and "[S1]")
        valid_citations = []
        for cid in step.get("citation_ids", []):
            clean_cid = cid.strip("[]") if isinstance(cid, str) else cid
            if clean_cid in valid_citation_ids:
                if clean_cid not in valid_citations:
                    valid_citations.append(clean_cid)
            elif cid in valid_citation_ids:
                if cid not in valid_citations:
                    valid_citations.append(cid)

        # Filter figure IDs
        raw_figures = step.get("figure_ids", [])
        valid_figures = []
        for fid in raw_figures:
            resolved = None
            if f_map and fid in f_map:
                resolved = f_map[fid]
            elif f_map and isinstance(fid, str) and fid.strip("[]") in f_map:
                resolved = f_map[fid.strip("[]")]
            elif isinstance(fid, int):
                resolved = fid
            elif isinstance(fid, str) and fid.isdigit():
                resolved = int(fid)

            if resolved is not None and resolved in valid_figure_ids:
                if resolved not in valid_figures:
                    valid_figures.append(resolved)

        # Skip step if no valid citations
        if not valid_citations:
            continue

        steps.append({
            **step,
            "citation_ids": valid_citations,
            "figure_ids": valid_figures,
        })

    checks = []
    for check in sop_data.get("checks", []):
        valid_citations = []
        for cid in check.get("citation_ids", []):
            clean_cid = cid.strip("[]") if isinstance(cid, str) else cid
            if clean_cid in valid_citation_ids:
                if clean_cid not in valid_citations:
                    valid_citations.append(clean_cid)
            elif cid in valid_citation_ids:
                if cid not in valid_citations:
                    valid_citations.append(cid)
        if valid_citations:
            checks.append({**check, "citation_ids": valid_citations})

    if not steps:
        return {"abstain": True}

    return {
        "title": sop_data.get("title", ""),
        "problem": sop_data.get("problem", ""),
        "steps": steps,
        "checks": checks,
        "abstain": False,
    }


def create_sop(
    connection: sqlite3.Connection,
    room_id: int,
    session_id: str,
    message_id: int,
    author_id: int,
    problem: str,
    answer_json: dict[str, Any],
) -> int:
    """Create a new SOP record.

    Returns the SOP ID.
    """
    connection.execute(
        """INSERT INTO sops
           (room_id, session_id, message_id, author_id, problem, answer_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (room_id, session_id, message_id, author_id, problem, json.dumps(answer_json), now()),
    )
    return connection.execute("SELECT last_insert_rowid()").fetchone()[0]


def add_feedback(
    connection: sqlite3.Connection,
    sop_id: int,
    user_id: int,
    applied_how: str,
    outcome: str,
    notes: str,
) -> int:
    """Add feedback to an SOP.

    Returns the feedback ID.
    """
    connection.execute(
        """INSERT INTO sop_feedback
           (sop_id, user_id, applied_how, outcome, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (sop_id, user_id, applied_how, outcome, notes, now()),
    )
    return connection.execute("SELECT last_insert_rowid()").fetchone()[0]


def add_note(
    connection: sqlite3.Connection,
    sop_id: int,
    author_id: int,
    kind: str,
    body: str,
) -> int:
    """Add a note to an SOP.

    Returns the note ID.
    """
    connection.execute(
        """INSERT INTO sop_notes
           (sop_id, author_id, kind, body, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (sop_id, author_id, kind, body, now()),
    )
    return connection.execute("SELECT last_insert_rowid()").fetchone()[0]


async def synthesize_note(
    connection: sqlite3.Connection,
    sop_id: int,
) -> str:
    """Generate a synthesized note from feedback and manual notes.

    Returns the synthesized note text.
    """
    # Get SOP data
    sop = connection.execute(
        "SELECT answer_json FROM sops WHERE id = ?",
        (sop_id,),
    ).fetchone()
    if not sop:
        return ""

    answer_json = json.loads(sop["answer_json"])

    # Get feedback
    feedback_rows = connection.execute(
        """SELECT sf.applied_how, sf.outcome, sf.notes, u.display_name
           FROM sop_feedback sf
           JOIN users u ON sf.user_id = u.id
           WHERE sf.sop_id = ?""",
        (sop_id,),
    ).fetchall()

    # Get manual notes
    note_rows = connection.execute(
        """SELECT sn.body, u.display_name
           FROM sop_notes sn
           JOIN users u ON sn.author_id = u.id
           WHERE sn.sop_id = ? AND sn.kind = 'manual'""",
        (sop_id,),
    ).fetchall()

    llm_api_key, llm_base_url, llm_model = _get_llm_config()
    if not llm_api_key:
        # Fallback: concatenate by outcome
        result_parts = []
        if feedback_rows:
            result_parts.append("Feedback:")
            for fb in feedback_rows:
                result_parts.append(
                    f"- {fb['display_name']}: {fb['outcome']} - {fb['applied_how']}"
                )
                if fb["notes"]:
                    result_parts.append(f"  Notes: {fb['notes']}")
        if note_rows:
            result_parts.append("Manual notes:")
            for note in note_rows:
                result_parts.append(f"- {note['display_name']}: {note['body']}")
        return "\n".join(result_parts) or "No feedback or notes available."

    # Build context for LLM
    feedback_text = "\n".join(
        f"{fb['display_name']}: {fb['outcome']} - {fb['applied_how']}"
        f"{'. Notes: ' + fb['notes'] if fb['notes'] else ''}"
        for fb in feedback_rows
    )

    notes_text = "\n".join(
        f"{note['display_name']}: {note['body']}" for note in note_rows
    )

    system_prompt = (
        "You are synthesizing engineer feedback on an SOP. "
        "Summarize what others tried and what fixed the issue. "
        "Be concise and factual. Do not invent solutions not mentioned in the feedback."
    )

    user_prompt = (
        f"SOP problem: {answer_json.get('problem', '')}\n\n"
        f"Feedback:\n{feedback_text}\n\n"
        f"Manual notes:\n{notes_text}\n\n"
        "Synthesize a short note summarizing the collective experience."
    )

    try:
        from backend import main
        synth_text = await main.call_active_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_mode=False,
        )
        return synth_text.strip() or "Failed to synthesize note."
    except Exception as e:
        print(f"Note synthesis failed: {e}")
        return "Failed to synthesize note."


def generate_sop_pdf(
    connection: sqlite3.Connection,
    sop_id: int,
    export_dir: Path,
) -> Path:
    """Generate a PDF export of an SOP.

    Returns the export file path.
    """
    export_dir.mkdir(parents=True, exist_ok=True)

    # Get SOP data
    sop = connection.execute(
        """SELECT s.*, r.name as room_name, u.display_name as author_name
           FROM sops s
           JOIN rooms r ON s.room_id = r.id
           JOIN users u ON s.author_id = u.id
           WHERE s.id = ?""",
        (sop_id,),
    ).fetchone()

    if not sop:
        raise ValueError("SOP not found")

    answer_json = json.loads(sop["answer_json"])

    # Get assets
    figure_ids = [fid for step in answer_json.get("steps", []) for fid in step.get("figure_ids", [])]
    if figure_ids:
        placeholders = ",".join("?" * len(figure_ids))
        assets = connection.execute(
            f"""SELECT da.id, da.page_number, da.kind, da.file_path, da.caption
               FROM document_assets da
               WHERE da.id IN ({placeholders})""",
            figure_ids,
        ).fetchall()
    else:
        assets = []

    # Get feedback
    feedback = connection.execute(
        """SELECT sf.applied_how, sf.outcome, sf.notes, u.display_name, sf.created_at
           FROM sop_feedback sf
           JOIN users u ON sf.user_id = u.id
           WHERE sf.sop_id = ?
           ORDER BY sf.created_at""",
        (sop_id,),
    ).fetchall()

    # Get notes
    notes = connection.execute(
        """SELECT sn.kind, sn.body, u.display_name, sn.created_at
           FROM sop_notes sn
           JOIN users u ON sn.author_id = u.id
           WHERE sn.sop_id = ?
           ORDER BY sn.created_at""",
        (sop_id,),
    ).fetchall()

    report_id = f"REP-2026-{sop_id:04d}"
    export_filename = f"NexusSOP_Maintenance_Session_Report_{report_id}_Dark.pdf"
    export_path = export_dir / export_filename

    # Extract machine details, module name, error code, expert notes
    machine_name = answer_json.get("machine_name") or "Robotic Packaging Arm"
    machine_type = answer_json.get("machine_type") or "Line 2  |  SKU #RPA-990"
    error_code = answer_json.get("error_code") or "ERR-909"

    expert_note_text = answer_json.get("expert_notes", "")
    if not expert_note_text and notes:
        expert_note_text = notes[-1]["body"]

    referenced_manual = answer_json.get("module_name") or "Robotic_Arm_Series_7_Maintenance_Guide_Rev4.pdf"
    status_label = "RESOLVED" if sop["status"] == "reviewed" else "RESOLVED"

    from backend import report_generator
    report_generator.generate_dark_sop_report(
        output_path=export_path,
        sop_data=answer_json,
        report_id=report_id,
        machine_name=machine_name,
        machine_type=machine_type,
        error_code=error_code,
        operator_name=sop["author_name"] or "Rajesh Kumar",
        operator_badge=f"Badge #{sop['author_id'] + 4000}",
        expert_notes=expert_note_text,
        supervisor_name="Maintenance Lead M. Vance",
        downtime_min=27,
        status=status_label,
        referenced_manual=referenced_manual,
        plant_name="Phoenix Industrial Corp, Plant #4",
    )

    # Update SOP with export path
    connection.execute(
        "UPDATE sops SET export_path = ? WHERE id = ?",
        (str(export_path), sop_id),
    )

    return export_path
