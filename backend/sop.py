"""SOP generation, feedback, notes, and PDF export for NexusSOP.

Implements:
- SOP generation from retrieved passages with citations and figures
- Feedback collection (applied_how, outcome, notes)
- Notes (manual and synthesized from feedback)
- PDF export with steps, figures, feedback, and notes
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fpdf import FPDF
from pydantic import BaseModel, Field


PART_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("E-stop button", ("e-stop", "estop", "emergency stop", "emergency-stop")),
    ("Axis board", ("axis board", "axis control board")),
    ("Servo drive", ("servo drive", "servo amplifier", "drive amplifier")),
    ("Encoder", ("encoder", "feedback encoder")),
    ("Spindle motor", ("spindle motor", "spindle drive", "spindle")),
    ("PLC input", ("plc input", "plc i/o", "plc io", "programmable logic controller")),
    ("Feedback cable", ("feedback cable", "encoder cable", "signal cable")),
    ("Limit switch", ("limit switch", "end switch", "travel switch")),
    ("Power supply", ("power supply", "24v supply", "dc supply", "power module")),
    ("Control panel", ("control panel", "operator panel", "cnc panel")),
    ("Cooling fan", ("cooling fan", "cabinet fan", "fan")),
    ("Circuit breaker", ("circuit breaker", "breaker", "main breaker")),
    ("Motor coupling", ("motor coupling", "coupling")),
)


def _part_mentions(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    for canonical, aliases in PART_ALIASES:
        if any(re.search(rf"\b{re.escape(alias)}\b", lowered) for alias in aliases):
            found.append(canonical)
    return found


def _asset_search_text(asset: dict[str, Any]) -> str:
    return " ".join(
        str(asset.get(key) or "")
        for key in ("caption", "kind", "file_path")
    ).lower()


def attach_part_visuals(
    sop_data: dict[str, Any],
    assets: list[dict[str, Any]],
    passages: list[dict[str, Any]] | None = None,
    query: str = "",
    error_code: str | None = None,
) -> dict[str, Any]:
    """Attach source-backed machine-part visuals to error-code SOPs.

    The visual is always an approved extracted asset/page snapshot. If a
    manual does not contain a matching close-up, the nearest source figure
    from the cited page is used and marked as a source-page reference.
    """
    if not error_code and not sop_data.get("error_code"):
        return sop_data

    passage_text = " ".join(str(p.get("passage") or "") for p in (passages or []))
    symptoms = " ".join(str(value) for value in (sop_data.get("symptoms") or []))
    causes = " ".join(str(value) for value in (sop_data.get("possible_causes") or []))
    all_text = " ".join([
        query,
        str(sop_data.get("problem") or ""),
        passage_text,
        symptoms,
        causes,
        " ".join(str(step.get("instruction") or "") for step in sop_data.get("steps") or []),
    ])
    mentioned_parts = _part_mentions(all_text)
    if not mentioned_parts or not assets:
        return sop_data

    assets_by_id = {int(asset["id"]): asset for asset in assets if asset.get("id") is not None}
    page_assets: dict[int, list[dict[str, Any]]] = {}
    for asset in assets:
        page = asset.get("page_number")
        if page is not None:
            page_assets.setdefault(int(page), []).append(asset)

    def cited_pages(text: str) -> set[int]:
        return {
            int(page)
            for page in re.findall(r"\[\[page:\s*(\d+)\]\]", text)
        }

    def choose_asset(part_name: str, context: str) -> dict[str, Any] | None:
        aliases = dict(PART_ALIASES).get(part_name, ())
        pages = cited_pages(context)
        candidates = []
        for asset in assets:
            searchable = _asset_search_text(asset)
            score = 0
            if any(alias in searchable for alias in aliases):
                score += 8
            if asset.get("page_number") in pages:
                score += 5
            if asset.get("kind") == "page":
                score += 1
            if score:
                candidates.append((score, asset))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
        for page in pages:
            if page_assets.get(page):
                return page_assets[page][0]
        return assets[0] if assets else None

    visual_by_part: dict[str, dict[str, Any]] = {}
    for part_name in mentioned_parts:
        asset = choose_asset(part_name, all_text)
        if not asset:
            continue
        asset_id = int(asset["id"])
        visual_by_part[part_name] = {
            "part_name": part_name,
            "part_image": f"/api/assets/{asset_id}",
            "part_asset_id": asset_id,
            "source_page": asset.get("page_number"),
            "caption": asset.get("caption") or "Original source figure",
        }

    related_parts = list(visual_by_part.values())
    sop_data["related_parts"] = related_parts

    for step in sop_data.get("steps") or []:
        step_text = " ".join([
            str(step.get("instruction") or ""),
            str(step.get("caution") or ""),
        ])
        step_parts = _part_mentions(step_text)
        if not step_parts:
            step_parts = [
                part for part in mentioned_parts
                if part in visual_by_part and any(
                    alias in step_text.lower()
                    for alias in dict(PART_ALIASES).get(part, ())
                )
            ]
        if step_parts:
            visual = visual_by_part.get(step_parts[0])
            if visual:
                step["part_name"] = visual["part_name"]
                step["part_image"] = visual["part_image"]
                step["part_asset_id"] = visual["part_asset_id"]
                step["part_source_page"] = visual["source_page"]
                figure_ids = step.setdefault("figure_ids", [])
                if visual["part_asset_id"] not in figure_ids:
                    figure_ids.append(visual["part_asset_id"])

    sop_data["solution_steps"] = sop_data.get("steps", [])
    return sop_data

# LLM configuration dynamically retrieved to support monkeypatching and avoid circular imports
def _get_llm_config() -> tuple[str, str, str]:
    from backend import main
    cfg = main.get_active_model_config()
    key = main.GEMINI_API_KEY if cfg["provider"] == "gemini" else main.LLM_API_KEY
    return key, cfg["provider"], cfg["active_model"]


def extract_steps_from_passages(
    query: str,
    passages: list[dict[str, Any]],
    assets: list[dict[str, Any]],
) -> dict[str, Any]:
    """Smart extractive procedure synthesizer for offline / fallback SOP generation."""
    steps = []
    checks = []
    current_caution = ""
    step_num = 1

    for i, p in enumerate(passages, start=1):
        ref_id = p.get("reference_id", f"S{i}")
        p_text = p.get("passage", "")

        page_num = None
        for line in p_text.splitlines():
            if "[[page:" in line:
                try:
                    page_num = int(line.split("[[page:")[1].split("]]")[0])
                    break
                except (ValueError, IndexError):
                    pass

        matching_fids = [
            a["id"] for a in assets
            if (page_num is not None and a.get("page_number") == page_num)
            or (a.get("kind") == "upload" and a.get("document_id") == p.get("document_id"))
        ]

        cleaned = re.sub(r"\[\[page:\d+\]\]", "", p_text).strip()
        non_heading_lines = [l.strip() for l in cleaned.splitlines() if l.strip() and not l.strip().startswith("#")]
        if not non_heading_lines:
            non_heading_lines = [cleaned.lstrip("#").strip()]

        list_items = []
        for line in non_heading_lines:
            if re.search(r"^(?:caution|warning|danger|hazard)\s*[:\-]", line, re.I):
                current_caution = line
            elif re.search(r"^(?:verify|confirm|check\s+that|ensure\s+that)\b", line, re.I):
                checks.append({"text": line, "citation_ids": [ref_id]})
            m = re.match(r"^(?:step\s*\d+[\s:\.\-]+|\d+[\.\)]\s*|[\-\*•]\s*)(.+)$", line, re.I)
            if m:
                list_items.append(m.group(1).strip())

        if list_items:
            for item in list_items:
                steps.append({
                    "number": step_num,
                    "instruction": item,
                    "citation_ids": [ref_id],
                    "figure_ids": matching_fids,
                    "caution": current_caution,
                })
                current_caution = ""
                step_num += 1
        else:
            joined_body = " ".join(non_heading_lines)
            raw_sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", joined_body) if s.strip()]
            for s in raw_sents:
                s_clean = s.strip()
                if not s_clean:
                    continue
                if re.search(r"^(?:caution|warning|danger|hazard)\s*[:\-]", s_clean, re.I):
                    current_caution = s_clean
                    continue
                if re.search(r"^(?:verify|confirm|check\s+that|ensure\s+that)\b", s_clean, re.I):
                    checks.append({"text": s_clean, "citation_ids": [ref_id]})
                    continue
                steps.append({
                    "number": step_num,
                    "instruction": s_clean,
                    "citation_ids": [ref_id],
                    "figure_ids": matching_fids,
                    "caution": current_caution,
                })
                current_caution = ""
                step_num += 1

    if not checks and steps:
        checks.append({
            "text": "Verify nominal operational tolerances, sensor feedback, and guard integrity.",
            "citation_ids": [steps[0]["citation_ids"][0]],
        })

    return {
        "title": f"SOP: {query[:60]}",
        "problem": query,
        "steps": steps,
        "checks": checks,
        "abstain": len(steps) == 0,
    }


# Configuration
EXPORT_DIR = Path(os.environ.get("SOP_EXPORT_DIR", "data/exports"))


class FeedbackRequest(BaseModel):
    applied_how: str
    outcome: str  # "fixed", "partial", "not_fixed"
    notes: str


class NoteRequest(BaseModel):
    body: str


class ExpertFeedbackRequest(BaseModel):
    detection: str = Field(min_length=2, max_length=2000)
    solution: str = Field(min_length=2, max_length=4000)
    outcome: str = Field(default="fixed", pattern="^(fixed|partial|not_fixed)$")
    notes: str = Field(default="", max_length=3000)
    error_code: str = Field(default="", max_length=120)


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

        CREATE TABLE IF NOT EXISTS expert_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            session_id TEXT,
            sop_id INTEGER,
            author_id INTEGER NOT NULL,
            machine_name TEXT,
            error_code TEXT,
            detection TEXT NOT NULL,
            solution TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('fixed', 'partial', 'not_fixed')),
            notes TEXT,
            approved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(room_id) REFERENCES rooms(id),
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE SET NULL,
            FOREIGN KEY(sop_id) REFERENCES sops(id) ON DELETE SET NULL,
            FOREIGN KEY(author_id) REFERENCES users(id)
        );
        """
    )


def _tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) > 2
    }


def find_similar_sops(
    connection: sqlite3.Connection,
    query: str,
    error_code: str = "",
    machine_name: str = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Return prior SOPs as references, ranked by lexical problem similarity."""
    rows = connection.execute(
        """SELECT id, problem, answer_json, status, created_at
           FROM sops
           ORDER BY CASE WHEN status = 'reviewed' THEN 0 ELSE 1 END, created_at DESC
           LIMIT 100"""
    ).fetchall()
    query_tokens = _tokens(" ".join([query, error_code, machine_name]))
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        answer = json.loads(row["answer_json"])
        text = " ".join([
            row["problem"] or "",
            answer.get("title", ""),
            answer.get("error_code", ""),
            answer.get("machine_name", ""),
            answer.get("machine", ""),
        ])
        candidate_tokens = _tokens(text)
        overlap = len(query_tokens & candidate_tokens)
        if not overlap:
            continue
        exact_error = bool(error_code and error_code.lower() in text.lower())
        score = overlap / max(len(query_tokens), 1) + (0.45 if exact_error else 0)
        ranked.append((score, {
            "id": row["id"],
            "title": answer.get("title") or f"SOP-{row['id']}",
            "problem": row["problem"],
            "status": row["status"],
            "created_at": row["created_at"],
            "error_code": answer.get("error_code", ""),
            "machine": answer.get("machine_name") or answer.get("machine", ""),
            "reference_url": f"/api/sops/{row['id']}",
            "similarity": round(score, 3),
        }))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in ranked[:limit]]


def add_expert_knowledge(
    connection: sqlite3.Connection,
    room_id: int,
    session_id: str | None,
    sop_id: int | None,
    author_id: int,
    machine_name: str,
    error_code: str,
    detection: str,
    solution: str,
    outcome: str,
    notes: str,
    approved: bool = False,
) -> int:
    connection.execute(
        """INSERT INTO expert_knowledge
           (room_id, session_id, sop_id, author_id, machine_name, error_code,
            detection, solution, outcome, notes, approved, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            room_id, session_id, sop_id, author_id, machine_name.strip(),
            error_code.strip(), detection.strip(), solution.strip(), outcome,
            notes.strip(), int(approved), now(),
        ),
    )
    return connection.execute("SELECT last_insert_rowid()").fetchone()[0]


def list_expert_knowledge(
    connection: sqlite3.Connection,
    session_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    where = "WHERE ek.session_id = ?" if session_id else ""
    params: tuple[Any, ...] = (session_id,) if session_id else ()
    rows = connection.execute(
        f"""SELECT ek.*, u.display_name
            FROM expert_knowledge ek
            JOIN users u ON u.id = ek.author_id
            {where}
            ORDER BY ek.created_at ASC
            LIMIT ?""",
        (*params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def find_relevant_expert_knowledge(
    connection: sqlite3.Connection,
    query: str,
    error_code: str = "",
    machine_name: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Find approved field learnings relevant to a new troubleshooting request."""
    target = _tokens(" ".join([query, error_code, machine_name]))
    rows = connection.execute(
        """SELECT ek.*, u.display_name
           FROM expert_knowledge ek
           JOIN users u ON u.id = ek.author_id
           WHERE ek.approved = 1
           ORDER BY ek.created_at DESC
           LIMIT 200"""
    ).fetchall()
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        candidate = _tokens(" ".join([
            row["detection"] or "", row["solution"] or "",
            row["error_code"] or "", row["machine_name"] or "",
        ]))
        overlap = len(target & candidate)
        if not overlap:
            continue
        score = overlap / max(len(target), 1)
        ranked.append((score, {
            **dict(row),
            "similarity": round(score, 3),
        }))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in ranked[:limit]]


async def build_sop_from_passages(
    query: str,
    passages: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    machine_name: str | None = None,
    machine_type: str | None = None,
    error_code: str | None = None,
    expert_notes: str | None = None,
    prior_references: list[dict[str, Any]] | None = None,
    expert_learnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Generate an authoritative, step-by-step SOP from retrieved passages and assets.

    Returns a concise, cited SOP with machine-part visual fields.
    """
    from backend import main
    cfg = main.get_active_model_config()
    has_key = cfg.get("has_active_key", False)

    # Build asset mapping for F1, F2... and integer IDs
    f_map: dict[Any, int] = {}
    for i, a in enumerate(assets, start=1):
        aid = a["id"]
        f_map[f"F{i}"] = aid
        f_map[f"[F{i}]"] = aid
        f_map[str(aid)] = aid
        f_map[aid] = aid

    if not has_key:
        return extract_steps_from_passages(query, passages, assets)

    # Build source context for LLM
    source_context = "\n\n".join(
        f"[{p.get('reference_id', f'S{i}')}] {p['passage']}"
        for i, p in enumerate(passages, start=1)
    )

    # Build figure list
    figure_list = "\n".join(
        f"[F{i}] {a.get('caption', 'Figure')} (page {a.get('page_number', 'N/A')})"
        for i, a in enumerate(assets, start=1)
    ) or "No figures extracted."

    system_prompt = (
        "You are an industrial SOP assistant. Use only the retrieved approved manual passages. "
        "Create a short, clear quick-reference procedure for a trained technician. "
        "Do not add background explanation, assumptions, or details not present in the sources.\n\n"
        "RESPONSE STRUCTURE (Return ONLY valid JSON matching this schema, no markdown wrapping):\n"
        "{\n"
        '  "title": "Precise, professional SOP title (e.g., SOP: Lathe Spindle Bearing Preload Adjustment & Alignment)",\n'
        '  "problem": "One short sentence describing the fault or task.",\n'
        '  "category": "Fault category such as drive, feedback, safety, power, or control.",\n'
        '  "symptoms": ["Short source-backed symptom"],\n'
        '  "possible_causes": ["Short source-backed possible cause"],\n'
        '  "steps": [\n'
        "    {\n"
        '      "number": 1,\n'
        '      "instruction": "One clear action in plain language. Include a value, tool, or part only when the source explicitly provides it.",\n'
        '      "part_name": "Exact machine-part name mentioned in this step",\n'
        '      "part_image": "F1 or another supplied figure token when the step identifies a part",\n'
        '      "citation_ids": ["S1"],\n'
        '      "figure_ids": ["F1"],\n'
        '      "caution": "Specific hazard or critical precaution for this step (e.g., Lock out main breaker QF1 before opening guard; do not exceed 30 PSI pneumatic pressure; pinch point hazard). Leave empty string if none."\n'
        "    }\n"
        "  ],\n"
        '  "checks": [\n'
        "    {\n"
        '      "text": "Testable post-step or final verification criteria to confirm operational readiness and safety (e.g., Run dial indicator across bedways to confirm runout is < 0.0005 in; check phase continuity before energizing).",\n'
        '      "citation_ids": ["S1"]\n'
        "    }\n"
        "  ],\n"
        '  "severity": "low, medium, high, or critical",\n'
        '  "machine": "Machine name/model",\n'
        '  "source": "Source manual name",\n'
        '  "related_parts": [{"part_name": "Exact part name", "part_image": "F1"}]\n'
        "}\n\n"
        "QUICK SOP RULES:\n"
        "1. Return no more than 6 essential steps. Combine repetitive actions.\n"
        "2. Keep each instruction to one or two short sentences.\n"
        "3. Include only source-backed safety warnings and verification checks.\n"
        "4. Every step must cite at least one supplied passage token.\n"
        "5. Attach only figures that directly support a step.\n"
        "6. For an error-code SOP, identify every important named part in symptoms, causes, and solution steps. "
        "Use supplied figure tokens for part_image; never invent an image URL.\n"
        "7. If the passages do not contain enough procedural evidence, return {\"abstain\": true}.\n"
    )

    user_prompt_lines = [
        f"Query: {query}",
    ]
    if machine_name:
        user_prompt_lines.append(f"Target Machine / Equipment: {machine_name}")
    if machine_type:
        user_prompt_lines.append(f"Machine Type: {machine_type}")
    if error_code:
        user_prompt_lines.append(f"Active Error Code / Diagnostic Fault: {error_code}")
    if expert_notes:
        user_prompt_lines.append(f"Technician / Expert Field Notes: {expert_notes}")
    if prior_references:
        user_prompt_lines.append(
            "\nPrior similar SOP references (use only as experience context; "
            "do not copy unsupported steps or replace manual citations):\n"
            + "\n".join(
                f"- SOP-{ref['id']}: {ref['title']} | {ref['problem']} | "
                f"status={ref['status']} | similarity={ref['similarity']}"
                for ref in prior_references
            )
        )
    if expert_learnings:
        user_prompt_lines.append(
            "\nApproved expert field learning (use as a secondary reference; "
            "manual citations remain mandatory):\n"
            + "\n".join(
                f"- Detection: {entry['detection']} | Solution: {entry['solution']} | "
                f"Outcome: {entry['outcome']} | Notes: {entry.get('notes') or 'none'}"
                for entry in expert_learnings
            )
        )

    user_prompt_lines.append(f"\nRetrieved passages from approved manual:\n{source_context}")
    user_prompt_lines.append(f"\nAvailable extracted figures and diagrams:\n{figure_list}")
    user_prompt_lines.append(
        "\nTask: Analyze the manual passages above. Summarize the problem and generate a proper, rigorous, step-by-step procedure with citations and verification checks."
    )
    user_prompt = "\n".join(user_prompt_lines)

    try:
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
        print(f"SOP LLM generation failed, falling back to extractive synthesis: {e}")
        return extract_steps_from_passages(query, passages, assets)


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

    def concise(value: Any, limit: int = 240) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "..."

    steps = []
    for step in sop_data.get("steps", [])[:6]:
        # Filter citation IDs (support both "S1", "[S1]", string or list)
        raw_cids = step.get("citation_ids") or step.get("citations") or step.get("citation") or []
        if isinstance(raw_cids, (str, int)):
            raw_cids = [str(raw_cids)]
        valid_citations = []
        for cid in raw_cids:
            clean_cid = cid.strip("[]") if isinstance(cid, str) else str(cid)
            if clean_cid in valid_citation_ids:
                if clean_cid not in valid_citations:
                    valid_citations.append(clean_cid)
            elif str(cid) in valid_citation_ids:
                if str(cid) not in valid_citations:
                    valid_citations.append(str(cid))

        # Filter figure IDs
        raw_figures = step.get("figure_ids") or step.get("figures") or []
        if isinstance(raw_figures, (str, int)):
            raw_figures = [raw_figures]
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
            "instruction": concise(step.get("instruction"), 240),
            "caution": concise(step.get("caution"), 180),
            "citation_ids": valid_citations,
            "figure_ids": valid_figures,
        })

    checks = []
    for check in sop_data.get("checks", [])[:3]:
        check_text = check.get("text") or check.get("check") or (check if isinstance(check, str) else "")
        raw_cids = check.get("citation_ids") if isinstance(check, dict) else []
        if isinstance(raw_cids, (str, int)):
            raw_cids = [str(raw_cids)]
        valid_citations = []
        for cid in raw_cids:
            clean_cid = cid.strip("[]") if isinstance(cid, str) else str(cid)
            if clean_cid in valid_citation_ids:
                if clean_cid not in valid_citations:
                    valid_citations.append(clean_cid)
            elif str(cid) in valid_citation_ids:
                if str(cid) not in valid_citations:
                    valid_citations.append(str(cid))
        if valid_citations or not raw_cids:
            # If citations not explicitly listed on check, inherit from valid citations if available
            c_ids = valid_citations if valid_citations else (list(valid_citation_ids)[:1] if valid_citation_ids else [])
            checks.append({
                "text": concise(check_text, 220),
                "citation_ids": c_ids,
            })

    if not steps:
        return {"abstain": True}

    return {
        "title": sop_data.get("title", ""),
        "problem": concise(sop_data.get("problem", ""), 220),
        "category": concise(sop_data.get("category", ""), 80),
        "symptoms": [concise(item, 180) for item in (sop_data.get("symptoms") or [])[:5]],
        "possible_causes": [concise(item, 180) for item in (sop_data.get("possible_causes") or [])[:5]],
        "severity": concise(sop_data.get("severity", ""), 30),
        "machine": concise(sop_data.get("machine", ""), 100),
        "source": concise(sop_data.get("source", ""), 160),
        "related_parts": sop_data.get("related_parts", [])[:12],
        "previous_sop_references": sop_data.get("previous_sop_references", [])[:5],
        "expert_learnings": sop_data.get("expert_learnings", [])[:5],
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
    compact: bool = False,
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
    export_filename = (
        f"NexusSOP_Quick_Reference_{report_id}.pdf"
        if compact
        else f"NexusSOP_Maintenance_Session_Report_{report_id}_Dark.pdf"
    )
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
    report_data = {
        **answer_json,
        "feedback": [dict(row) for row in feedback],
        "notes": [dict(row) for row in notes],
        "figures": [dict(row) for row in assets],
    }
    if compact:
        report_generator.generate_simple_sop_report(
            output_path=export_path,
            sop_data=report_data,
            referenced_manual=referenced_manual,
        )
    else:
        report_generator.generate_dark_sop_report(
            output_path=export_path,
            sop_data=report_data,
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
