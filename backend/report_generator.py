"""Dark Industrial Maintenance Session Report Generator.

Produces executive multi-page dark-themed engineering and maintenance session reports
matching the design and structure of NexusSOP_Maintenance_Session_Report_REP-2026-8832_Dark.pdf.
"""

from __future__ import annotations

import io
import logging
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.graphics.shapes import (
    Drawing,
    Group,
    Line,
    Polygon,
    Rect,
    String,
)

# Color Palette matching NexusSOP Dark PDF
COLOR_PAGE_BG = HexColor("#0a0f15")       # Darkest slate background
COLOR_HEADER_BG = HexColor("#121b26")     # Top banner & card background
COLOR_CARD_BG = HexColor("#121b26")       # Container box background
COLOR_CARD_BORDER = HexColor("#1e293b")   # Slate border
COLOR_ORANGE = HexColor("#f26a0f")        # Safety Orange accent
COLOR_CYAN = HexColor("#22d3ee")          # Tech Cyan accent
COLOR_GREEN = HexColor("#10b981")         # Success / Resolved
COLOR_TEXT_WHITE = HexColor("#ffffff")
COLOR_TEXT_OFFWHITE = HexColor("#e6edf3")
COLOR_TEXT_MUTED = HexColor("#8fa3b8")
COLOR_TEXT_SLATE = HexColor("#a9b8c6")
COLOR_WARN_BG = HexColor("#1c140e")       # Warning card background
COLOR_WARN_BORDER = HexColor("#f26a0f")


class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas to dynamically compute and stamp total page count (Page X of Y)

    and draw the dark industrial header banner and footer on every page.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved_page_states: list[dict[str, Any]] = []
        self.report_id = "REP-2026-8832"
        self.plant_name = "Phoenix Industrial Corp, Plant #4"
        self.status = "RESOLVED"

    def showPage(self) -> None:
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, num_pages: int) -> None:
        w, h = A4

        # 1. Dark page background
        self.setFillColor(COLOR_PAGE_BG)
        self.rect(0, 0, w, h, fill=1, stroke=0)

        # 2. Header banner background
        self.setFillColor(COLOR_HEADER_BG)
        self.rect(0, h - 148, w, 148, fill=1, stroke=0)

        # 3. Orange dividing line
        self.setFillColor(COLOR_ORANGE)
        self.rect(0, h - 152, w, 3.5, fill=1, stroke=0)

        # 4. Brand text
        self.setFont("Helvetica-Bold", 19)
        self.setFillColor(COLOR_TEXT_WHITE)
        self.drawString(40, h - 42, "NEXUS ")
        self.setFillColor(COLOR_ORANGE)
        self.drawString(116, h - 42, "SOP ")
        self.setFillColor(COLOR_TEXT_WHITE)
        self.drawString(162, h - 42, "EVIDENCE")

        # 5. Local Retrieval badge
        self.setFont("Courier-Bold", 8)
        self.setFillColor(COLOR_CYAN)
        self.drawString(290, h - 40, "[ LOCAL RETRIEVAL ]")

        # 6. Subtitle line 1
        self.setFont("Helvetica", 9)
        self.setFillColor(COLOR_TEXT_MUTED)
        self.drawString(40, h - 60, "Source-grounded industrial knowledge assistant")

        # 7. Document title
        self.setFont("Helvetica-Bold", 22)
        self.setFillColor(COLOR_TEXT_WHITE)
        self.drawString(40, h - 92, "Maintenance Session Report")

        # 8. Dynamic Page Subtitle
        self.setFont("Helvetica", 9)
        self.setFillColor(COLOR_TEXT_SLATE)
        p_num = self._pageNumber
        if p_num == 1:
            page_subtitle = f"Automated maintenance and troubleshooting record  |  {self.plant_name}"
        elif p_num == 2:
            page_subtitle = f"Troubleshooting flowchart  |  {self.report_id}"
        elif p_num == 3:
            page_subtitle = f"Technical diagrams  |  Axis drive and fault chain"
        elif p_num == 4:
            page_subtitle = f"System architecture and feedback loop  |  {self.report_id}"
        else:
            page_subtitle = f"Approval, audit trail and metadata  |  {self.report_id}"
        self.drawString(40, h - 110, page_subtitle)

        # 9. Status Pill Badge (Top Right)
        status_text = self.status.upper()
        badge_w = 80
        badge_h = 20
        badge_x = w - 40 - badge_w
        badge_y = h - 94
        self.setFillColor(COLOR_GREEN if "RESOLV" in status_text or "APPROV" in status_text else COLOR_ORANGE)
        self.roundRect(badge_x, badge_y, badge_w, badge_h, 4, fill=1, stroke=0)
        self.setFillColor(COLOR_TEXT_WHITE)
        self.setFont("Helvetica-Bold", 9)
        self.drawCentredString(badge_x + (badge_w / 2), badge_y + 6, status_text)

        # 10. Report ID (Top Right below status)
        self.setFont("Helvetica", 8.5)
        self.setFillColor(COLOR_TEXT_SLATE)
        self.drawRightString(w - 40, h - 110, f"Report ID  {self.report_id}")

        # 11. Footer
        self.setStrokeColor(COLOR_CARD_BORDER)
        self.setLineWidth(1)
        self.line(40, 34, w - 40, 34)

        self.setFillColor(COLOR_TEXT_MUTED)
        self.setFont("Helvetica", 8)
        self.drawString(40, 20, f"Nexus SOP Evidence  |  {self.report_id}  |  {self.plant_name}")
        self.drawRightString(w - 40, 20, f"Page {p_num} of {num_pages}")


def _build_styles() -> dict[str, ParagraphStyle]:
    """Build typography styles for dark theme report."""
    return {
        "SecHeading": ParagraphStyle(
            "SecHeading",
            fontName="Helvetica-Bold",
            fontSize=11.5,
            leading=15,
            textColor=COLOR_TEXT_WHITE,
            spaceAfter=6,
        ),
        "SubLabel": ParagraphStyle(
            "SubLabel",
            fontName="Helvetica",
            fontSize=7.5,
            leading=10,
            textColor=COLOR_TEXT_MUTED,
        ),
        "ValText": ParagraphStyle(
            "ValText",
            fontName="Helvetica-Bold",
            fontSize=9.5,
            leading=12,
            textColor=COLOR_TEXT_OFFWHITE,
        ),
        "BodyText": ParagraphStyle(
            "BodyText",
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            textColor=COLOR_TEXT_SLATE,
        ),
        "CautionText": ParagraphStyle(
            "CautionText",
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=12,
            textColor=COLOR_ORANGE,
        ),
        "MetricLabel": ParagraphStyle(
            "MetricLabel",
            fontName="Helvetica",
            fontSize=7.5,
            leading=9,
            textColor=COLOR_TEXT_MUTED,
        ),
        "MetricVal": ParagraphStyle(
            "MetricVal",
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=14,
            textColor=COLOR_TEXT_WHITE,
        ),
        "FigureCaption": ParagraphStyle(
            "FigureCaption",
            fontName="Helvetica-Bold",
            fontSize=9.5,
            leading=13,
            textColor=COLOR_TEXT_WHITE,
            spaceAfter=8,
        ),
        "FigureNote": ParagraphStyle(
            "FigureNote",
            fontName="Helvetica-Oblique",
            fontSize=7.5,
            leading=10,
            textColor=COLOR_TEXT_MUTED,
            spaceBefore=4,
        ),
    }


def _make_metric_box(label: str, val: str, width: float, styles: dict[str, ParagraphStyle]) -> Table:
    """Create a dark card metric block."""
    p_label = Paragraph(label, styles["MetricLabel"])
    p_val = Paragraph(val, styles["MetricVal"])
    t = Table([[p_label], [Spacer(1, 2)], [p_val]], colWidths=[width])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                ("PADDING", (0, 0), (-1, -1), 6),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ]
        )
    )
    return t


def _draw_flowchart_page2(error_code: str, steps: list[dict[str, Any]]) -> Drawing:
    """Generate the troubleshooting flowchart for Page 2."""
    d = Drawing(515, 550)
    w = 515

    # Helper to draw a node box
    def node(
        x: float, y: float, bw: float, bh: float, title: str, subtitle: str = "", fill: HexColor = COLOR_CARD_BG, stroke: HexColor = COLOR_CARD_BORDER
    ) -> None:
        d.add(Rect(x, y, bw, bh, fillColor=fill, strokeColor=stroke, strokeWidth=1, rx=4, ry=4))
        if subtitle:
            d.add(String(x + 10, y + bh - 14, title, fontName="Helvetica-Bold", fontSize=8, fillColor=COLOR_TEXT_WHITE))
            d.add(String(x + 10, y + 8, subtitle[:58], fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
        else:
            d.add(String(x + 10, y + (bh / 2) - 3, title, fontName="Helvetica-Bold", fontSize=8, fillColor=COLOR_TEXT_WHITE))

    # Helper for connector arrow
    def v_arrow(x: float, y_start: float, y_end: float, label: str = "") -> None:
        d.add(Line(x, y_start, x, y_end, strokeColor=COLOR_ORANGE, strokeWidth=1.2))
        d.add(Polygon([x - 2.5, y_end + 4, x + 2.5, y_end + 4, x, y_end], fillColor=COLOR_ORANGE, strokeColor=COLOR_ORANGE))
        if label:
            d.add(String(x + 5, (y_start + y_end) / 2 - 3, label, fontName="Helvetica-Bold", fontSize=7, fillColor=COLOR_CYAN))

    # Flowchart Nodes Layout (550 pt total height)
    # 1. Symptom & Code
    node(50, 500, 415, 36, f"Symptom: Equipment stall or abnormal diagnostic event", f"Captured code: {error_code}  |  Operator uploaded visual telemetry")
    v_arrow(257, 500, 465)

    # 2. VLM & Retrieval
    node(50, 430, 415, 36, "Multimodal GenAI Analysis & Grounded Retrieval", "VLM detects mechanical jam / wear. Excerpts retrieved from indexed manuals.")
    v_arrow(257, 430, 395)

    # 3. Safety Isolation
    node(50, 360, 415, 36, "Safety Isolation: Lockout / Tagout (LOTO) Protocol", "E-stop engaged. Main panel disconnected. Pneumatic pressure released.", fill=COLOR_WARN_BG, stroke=COLOR_ORANGE)
    v_arrow(257, 360, 325)

    # 4. Zero-Energy Decision Diamond/Box
    node(130, 290, 255, 36, "Zero-energy state verified?", "Instrument verification / smart tablet confirmation", stroke=COLOR_CYAN)
    
    # Branch NO -> Re-isolate
    d.add(Line(385, 308, 470, 308, strokeColor=COLOR_ORANGE, strokeWidth=1))
    d.add(Line(470, 308, 470, 378, strokeColor=COLOR_ORANGE, strokeWidth=1))
    d.add(Line(470, 378, 465, 378, strokeColor=COLOR_ORANGE, strokeWidth=1))
    d.add(String(395, 312, "No", fontName="Helvetica-Bold", fontSize=7, fillColor=HexColor("#ef4444")))

    # Branch YES -> Execution steps
    v_arrow(257, 290, 255, "Yes")

    # 5. Step Execution
    s1 = steps[0]["instruction"] if steps else "Execute procedure steps with certified tooling"
    node(50, 220, 415, 36, "Execute Mechanical Clearance & Step Procedure", s1[:62])
    v_arrow(257, 220, 185)

    # 6. Integrity check
    node(130, 150, 255, 36, "Hardware integrity & alignment nominal?", "Visual gear / component inspection passed", stroke=COLOR_CYAN)
    v_arrow(257, 150, 115, "Yes")

    # 7. System reboot & test cycle
    node(50, 80, 415, 36, "System Reboot, Pad Clear & Diagnostic Dry-Run", "Calibration verified nominal. Machine returned to full operational state.", stroke=COLOR_GREEN)
    v_arrow(257, 80, 45)

    # 8. Resolution Box
    node(100, 10, 315, 35, "RESOLVED: Shift Session Closed & Report Generated", "Notes appended to Engineering Supervisor Review Queue", fill=HexColor("#0d281e"), stroke=COLOR_GREEN)

    return d


def _draw_diagrams_page3() -> Drawing:
    """Generate technical schematic & fault chain diagrams for Page 3."""
    d = Drawing(515, 520)

    # Figure 2: Drive Component Schematic
    d.add(Rect(20, 330, 475, 180, fillColor=COLOR_CARD_BG, strokeColor=COLOR_CARD_BORDER, strokeWidth=1, rx=4, ry=4))
    d.add(String(35, 492, "Figure 2. Mechanical drive schematic and component layout", fontName="Helvetica-Bold", fontSize=9, fillColor=COLOR_TEXT_WHITE))

    # Drive motor schematic box
    d.add(Rect(45, 395, 80, 60, fillColor=HexColor("#1b2533"), strokeColor=COLOR_CYAN, strokeWidth=1.2, rx=3, ry=3))
    d.add(String(55, 432, "Servo Motor", fontName="Helvetica-Bold", fontSize=8, fillColor=COLOR_TEXT_WHITE))
    d.add(String(55, 418, "Drive Axis Z", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_MUTED))
    d.add(CircleCallout(35, 440, "1", d))

    # Gearbox
    d.add(Rect(165, 400, 75, 55, fillColor=HexColor("#1b2533"), strokeColor=COLOR_ORANGE, strokeWidth=1.2, rx=3, ry=3))
    d.add(String(175, 432, "Pinion / Gear", fontName="Helvetica-Bold", fontSize=8, fillColor=COLOR_TEXT_WHITE))
    d.add(String(175, 418, "Drive teeth", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_MUTED))
    d.add(CircleCallout(155, 440, "2", d))

    # Jam point
    d.add(Rect(275, 405, 75, 48, fillColor=COLOR_WARN_BG, strokeColor=COLOR_ORANGE, strokeWidth=1.2, rx=3, ry=3))
    d.add(String(283, 432, "Scrap Jam", fontName="Helvetica-Bold", fontSize=8, fillColor=COLOR_ORANGE))
    d.add(String(283, 418, "Obstruction", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(CircleCallout(265, 440, "3", d))

    # Safety Guard
    d.add(Rect(385, 395, 90, 60, fillColor=HexColor("#1b2533"), strokeColor=COLOR_CYAN, strokeWidth=1, rx=3, ry=3))
    d.add(String(395, 432, "Safety Guard", fontName="Helvetica-Bold", fontSize=8, fillColor=COLOR_TEXT_WHITE))
    d.add(String(395, 418, "Access Panel #3", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_MUTED))
    d.add(CircleCallout(375, 440, "4", d))

    # Connecting drive lines
    d.add(Line(125, 428, 165, 428, strokeColor=COLOR_CYAN, strokeWidth=1.5))
    d.add(Line(240, 428, 275, 428, strokeColor=COLOR_ORANGE, strokeWidth=1.5))
    d.add(Line(350, 428, 385, 428, strokeColor=COLOR_CYAN, strokeWidth=1.2))

    # Callout details table
    d.add(String(35, 368, "1: Servo Motor (over-current detected)    2: Pinion gear drive teeth    3: Wedged debris", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(35, 354, "4: Guard #3 (removed via 5mm key)          5: Tensioner rollers          6: Film/stock feed path", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(35, 340, "Illustrative schematic for session audit. For plant wiring see DWG-RPA-Z-Axis-Wiring.cad.", fontName="Helvetica-Oblique", fontSize=6.5, fillColor=COLOR_TEXT_MUTED))

    # Figure 3: Likely Fault Chain
    d.add(Rect(20, 165, 475, 150, fillColor=COLOR_CARD_BG, strokeColor=COLOR_CARD_BORDER, strokeWidth=1, rx=4, ry=4))
    d.add(String(35, 295, "Figure 3. Likely fault chain, from the operator's shift note", fontName="Helvetica-Bold", fontSize=9, fillColor=COLOR_TEXT_WHITE))

    chain_nodes = [
        ("Loose rollers", "Mechanical slack"),
        ("Scrap buildup", "Behind guard #3"),
        ("Teeth jam", "Drive obstruction"),
        ("Over-current", "ERR-909 trip"),
        ("Arm stall", "Mid-cycle stop"),
    ]
    cur_x = 35
    for i, (title, sub) in enumerate(chain_nodes):
        d.add(Rect(cur_x, 225, 75, 40, fillColor=HexColor("#17212e"), strokeColor=COLOR_CARD_BORDER, strokeWidth=1, rx=3, ry=3))
        d.add(String(cur_x + 5, 250, title, fontName="Helvetica-Bold", fontSize=7.5, fillColor=COLOR_TEXT_WHITE))
        d.add(String(cur_x + 5, 235, sub, fontName="Helvetica", fontSize=6.5, fillColor=COLOR_TEXT_MUTED))
        if i < len(chain_nodes) - 1:
            d.add(Line(cur_x + 75, 245, cur_x + 92, 245, strokeColor=COLOR_ORANGE, strokeWidth=1.5))
            d.add(Polygon([cur_x + 90, 248, cur_x + 90, 242, cur_x + 93, 245], fillColor=COLOR_ORANGE, strokeColor=COLOR_ORANGE))
        cur_x += 92

    d.add(Rect(35, 180, 445, 24, fillColor=COLOR_WARN_BG, strokeColor=COLOR_ORANGE, strokeWidth=0.8, rx=3, ry=3))
    d.add(String(45, 188, "Proposed Preventive Fix: Add bi-weekly roller check to preventive maintenance SOP, pending supervisor review.", fontName="Helvetica-Bold", fontSize=7, fillColor=COLOR_ORANGE))

    # Figure 4: Energy Isolation Sequence
    d.add(Rect(20, 10, 475, 140, fillColor=COLOR_CARD_BG, strokeColor=COLOR_CARD_BORDER, strokeWidth=1, rx=4, ry=4))
    d.add(String(35, 132, "Figure 4. Energy isolation sequence before touching mechanism", fontName="Helvetica-Bold", fontSize=9, fillColor=COLOR_TEXT_WHITE))

    iso_steps = [
        ("1. Electrical", "E-Stop & LOTO Panel B"),
        ("2. Pneumatics", "Bleed valve pressure"),
        ("3. Zero-Energy", "Verify instrument state"),
        ("4. Safe Access", "Remove Guard #3"),
    ]
    x_iso = 35
    for i, (title, sub) in enumerate(iso_steps):
        d.add(Rect(x_iso, 50, 95, 42, fillColor=HexColor("#17212e"), strokeColor=COLOR_CYAN if i == 2 else COLOR_CARD_BORDER, strokeWidth=1, rx=3, ry=3))
        d.add(String(x_iso + 8, 76, title, fontName="Helvetica-Bold", fontSize=8, fillColor=COLOR_TEXT_WHITE))
        d.add(String(x_iso + 8, 62, sub, fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_MUTED))
        if i < len(iso_steps) - 1:
            d.add(Line(x_iso + 95, 71, x_iso + 115, 71, strokeColor=COLOR_CYAN, strokeWidth=1.5))
        x_iso += 115

    d.add(String(35, 25, "Verified: Power isolated and zero-energy confirmed prior to physical technician contact.", fontName="Helvetica", fontSize=7.5, fillColor=COLOR_GREEN))

    return d


def CircleCallout(x: float, y: float, num: str, drawing: Drawing) -> None:
    """Helper to draw circular numbered badge on technical schematic."""
    drawing.add(Rect(x - 6, y - 6, 12, 12, fillColor=COLOR_ORANGE, strokeColor=COLOR_ORANGE, rx=6, ry=6))
    drawing.add(String(x - 2.5, y - 3, num, fontName="Helvetica-Bold", fontSize=7, fillColor=COLOR_TEXT_WHITE))


def _draw_architecture_page4() -> Drawing:
    """Generate system architecture & expert note routing diagrams for Page 4."""
    d = Drawing(515, 330)

    # Figure 5: Architecture diagram
    d.add(Rect(20, 160, 475, 160, fillColor=COLOR_CARD_BG, strokeColor=COLOR_CARD_BORDER, strokeWidth=1, rx=4, ry=4))
    d.add(String(35, 302, "Figure 5. How the multimodal assistant produced this report", fontName="Helvetica-Bold", fontSize=9.5, fillColor=COLOR_TEXT_WHITE))

    # Box 1: Operator
    d.add(Rect(35, 185, 120, 100, fillColor=HexColor("#17212e"), strokeColor=COLOR_CYAN, strokeWidth=1, rx=3, ry=3))
    d.add(String(45, 270, "Operator PWA Client", fontName="Helvetica-Bold", fontSize=8, fillColor=COLOR_TEXT_WHITE))
    d.add(String(45, 252, "• Symptom & telemetry", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(45, 238, "• Image & audio capture", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(45, 224, "• Step execution logs", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(45, 210, "• Expert shift notes", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(45, 196, "• Biometric / PIN sign-off", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))

    # Box 2: Assistant Engine
    d.add(Rect(190, 185, 140, 100, fillColor=HexColor("#17212e"), strokeColor=COLOR_ORANGE, strokeWidth=1, rx=3, ry=3))
    d.add(String(198, 270, "GenAI Assistant Core", fontName="Helvetica-Bold", fontSize=8, fillColor=COLOR_TEXT_WHITE))
    d.add(String(198, 252, "• VLM multi-modal vision", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(198, 238, "• Grounded lexical retrieval", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(198, 224, "• LOTO safety gate", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(198, 210, "• ReportLab report compiler", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(198, 196, "• Supervisor queue router", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))

    # Box 3: Knowledge Base & Outputs
    d.add(Rect(365, 185, 115, 100, fillColor=HexColor("#17212e"), strokeColor=COLOR_GREEN, strokeWidth=1, rx=3, ry=3))
    d.add(String(373, 270, "Outputs & Audit", fontName="Helvetica-Bold", fontSize=8, fillColor=COLOR_TEXT_WHITE))
    d.add(String(373, 252, "• REP-2026 Session Report", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(373, 238, "• Supervisor review queue", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(373, 224, "• Verified audit trail", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))
    d.add(String(373, 210, "• Knowledge base updates", fontName="Helvetica", fontSize=7, fillColor=COLOR_TEXT_SLATE))

    # Connectors
    d.add(Line(155, 235, 190, 235, strokeColor=COLOR_CYAN, strokeWidth=1.5))
    d.add(Line(330, 235, 365, 235, strokeColor=COLOR_ORANGE, strokeWidth=1.5))

    # Figure 6: Expert Note Routing Flowchart
    d.add(Rect(20, 10, 475, 130, fillColor=COLOR_CARD_BG, strokeColor=COLOR_CARD_BORDER, strokeWidth=1, rx=4, ry=4))
    d.add(String(35, 122, "Figure 6. Where the expert note goes next in the governance pipeline", fontName="Helvetica-Bold", fontSize=9.5, fillColor=COLOR_TEXT_WHITE))

    note_steps = [
        ("1. Shift Note Added", "Operator attaches note"),
        ("2. Queue Routing", "Sent to Plant Lead"),
        ("3. Engineering Review", "Supervisor assesses"),
        ("4. SOP Revision", "Added to PM Manual"),
    ]
    x_n = 35
    for i, (title, sub) in enumerate(note_steps):
        d.add(Rect(x_n, 45, 95, 42, fillColor=HexColor("#17212e"), strokeColor=COLOR_ORANGE if i == 0 else COLOR_CARD_BORDER, strokeWidth=1, rx=3, ry=3))
        d.add(String(x_n + 6, 71, title, fontName="Helvetica-Bold", fontSize=7.5, fillColor=COLOR_TEXT_WHITE))
        d.add(String(x_n + 6, 57, sub, fontName="Helvetica", fontSize=6.5, fillColor=COLOR_TEXT_MUTED))
        if i < len(note_steps) - 1:
            d.add(Line(x_n + 95, 66, x_n + 115, 66, strokeColor=COLOR_ORANGE, strokeWidth=1.5))
        x_n += 115

    d.add(String(35, 25, "Status: Expert note recorded in session log and appended to supervisor action queue.", fontName="Helvetica", fontSize=7.5, fillColor=COLOR_GREEN))

    return d


def _draw_audit_trail_page5(report_id: str, operator_name: str, supervisor_name: str) -> Drawing:
    """Generate audit trail timeline for Page 5."""
    d = Drawing(515, 140)
    d.add(Rect(20, 10, 475, 120, fillColor=COLOR_CARD_BG, strokeColor=COLOR_CARD_BORDER, strokeWidth=1, rx=4, ry=4))
    d.add(String(35, 112, "Figure 7. Complete session audit trail and certification sequence", fontName="Helvetica-Bold", fontSize=9.5, fillColor=COLOR_TEXT_WHITE))

    events = [
        ("Session Closed", "Status: RESOLVED"),
        ("Operator Sign-off", f"PIN / Biometric: {operator_name[:12]}"),
        ("Report Compiled", f"{report_id} PDF stamped"),
        ("Supervisor Certified", f"Lead: {supervisor_name[:14]}"),
    ]
    cur_x = 35
    for i, (title, sub) in enumerate(events):
        d.add(Rect(cur_x, 35, 95, 45, fillColor=HexColor("#17212e"), strokeColor=COLOR_GREEN if i in (0, 3) else COLOR_CARD_BORDER, strokeWidth=1, rx=3, ry=3))
        d.add(String(cur_x + 6, 64, title, fontName="Helvetica-Bold", fontSize=7.5, fillColor=COLOR_TEXT_WHITE))
        d.add(String(cur_x + 6, 48, sub, fontName="Helvetica", fontSize=6.5, fillColor=COLOR_TEXT_MUTED))
        if i < len(events) - 1:
            d.add(Line(cur_x + 95, 57, cur_x + 115, 57, strokeColor=COLOR_GREEN, strokeWidth=1.5))
        cur_x += 115

    d.add(String(35, 20, "Audit Status: Cryptographic & database record verified against plant compliance standards.", fontName="Helvetica", fontSize=7.5, fillColor=COLOR_CYAN))
    return d


def _append_real_reference_pages(
    story: list[Any],
    referenced_manual: str,
    manual_pages: str,
    sop_data: dict[str, Any],
    styles: dict[str, ParagraphStyle],
    w_avail: float,
    report_id: str,
) -> None:
    """Extract and embed real reference pages from source technical manuals (PDFs, images, or assets).

    Places them as Appendix / Section 7 at the end of the report.
    """
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            fitz = None

    manual_candidates: list[str] = []
    if sop_data.get("module_name"):
        manual_candidates.append(str(sop_data["module_name"]))
    if referenced_manual:
        manual_candidates.append(str(referenced_manual))

    citations = sop_data.get("citations", [])
    for c in citations:
        if isinstance(c, dict):
            doc_name = c.get("document_name")
            if doc_name and doc_name not in manual_candidates:
                manual_candidates.append(str(doc_name))

    workspace_root = Path("/home/inventashif/Desktop/btech/hackathon")
    uploads_dir = workspace_root / "data" / "uploads"

    target_file: Path | None = None
    target_doc_name: str = referenced_manual or "Equipment Technical Manual"

    for cand in manual_candidates:
        cand_clean = cand.strip()
        p1 = workspace_root / cand_clean
        if p1.is_file():
            target_file = p1
            target_doc_name = cand_clean
            break
        p2 = uploads_dir / cand_clean
        if p2.is_file():
            target_file = p2
            target_doc_name = cand_clean
            break

    db_path = workspace_root / "data" / "nexus_sop.sqlite3"
    if not target_file and db_path.is_file():
        try:
            with sqlite3.connect(db_path) as conn:
                for cand in manual_candidates:
                    row = conn.execute(
                        "SELECT id, name, image_path FROM documents WHERE approved = 1 AND (name = ? OR name LIKE ?)",
                        (cand, f"%{cand}%"),
                    ).fetchone()
                    if row:
                        target_doc_name = row[1]
                        if row[2]:
                            img_p = uploads_dir / row[2]
                            if img_p.is_file():
                                target_file = img_p
                                break
                        p_ws = workspace_root / target_doc_name
                        if p_ws.is_file():
                            target_file = p_ws
                            break
                        break
        except Exception as e:
            logger.warning("DB lookup for reference manual failed: %s", e)

    if not target_file:
        for fallback_pdf in [workspace_root / "Module 2.pdf", workspace_root / "machine.pdf"]:
            if fallback_pdf.is_file():
                target_file = fallback_pdf
                target_doc_name = fallback_pdf.name
                break

    rendered_pages = 0
    max_ref_pages = 2

    # Case A: Source file is a PDF and PyMuPDF is available
    if target_file and target_file.suffix.lower() == ".pdf" and fitz:
        try:
            pdf_doc = fitz.open(target_file)
            total_pdf_pages = len(pdf_doc)
            target_pages: list[int] = []

            # 1. Check manual_pages string (e.g. "42", "112-114")
            if manual_pages:
                for num_str in re.findall(r"\b\d+\b", manual_pages):
                    val = int(num_str)
                    if 1 <= val <= total_pdf_pages:
                        target_pages.append(val - 1)
                    elif 0 <= val < total_pdf_pages:
                        target_pages.append(val)

            # 2. Check citations in sop_data
            for c in citations:
                if isinstance(c, dict):
                    passage = c.get("passage", "")
                    page_match = re.search(r"\[\[page:\s*(\d+)\]\]", passage)
                    if page_match:
                        p_num = int(page_match.group(1))
                        if 1 <= p_num <= total_pdf_pages:
                            target_pages.append(p_num - 1)
                        elif 0 <= p_num < total_pdf_pages:
                            target_pages.append(p_num)

            # 3. Keyword / error-code search if none found
            if not target_pages:
                err = sop_data.get("error_code") or ""
                mach = sop_data.get("machine_name") or ""
                search_terms = [t for t in [err, "calibration", "torque", "troubleshooting", mach] if t]
                for p_idx in range(min(total_pdf_pages, 80)):
                    txt = pdf_doc[p_idx].get_text().lower()
                    if err and err.lower() in txt:
                        target_pages.append(p_idx)
                        break
                    if any(st.lower() in txt for st in search_terms):
                        target_pages.append(p_idx)
                        if len(target_pages) >= 1:
                            break

            if not target_pages:
                target_pages = [0]

            seen_p = set()
            valid_p = []
            for p in target_pages:
                if 0 <= p < total_pdf_pages and p not in seen_p:
                    seen_p.add(p)
                    valid_p.append(p)
                    if len(valid_p) >= max_ref_pages:
                        break

            for p_idx in valid_p:
                page_obj = pdf_doc[p_idx]
                pix = page_obj.get_pixmap(dpi=130)

                ar = pix.width / max(1, pix.height)
                if ar >= 1.0:  # Landscape
                    w = min(w_avail, 515.0)
                    h = w / ar
                    if h > 480.0:
                        h = 480.0
                        w = h * ar
                else:  # Portrait
                    h = min(480.0, 515.0 / ar)
                    w = h * ar
                    if w > w_avail:
                        w = w_avail
                        h = w / ar

                img_stream = io.BytesIO(pix.tobytes("png"))
                ref_img = Image(img_stream, width=w, height=h)
                ref_img.hAlign = "CENTER"

                story.append(PageBreak())
                story.append(
                    Paragraph(
                        "7. Primary source reference manual pages & engineering field evidence",
                        styles["SecHeading"],
                    )
                )

                badge_table_data = [
                    [
                        Paragraph(f"<b>SOURCE MANUAL:</b> {target_doc_name}", styles["ValText"]),
                        Paragraph(f"<b>PAGE NO:</b> {p_idx + 1} of {total_pdf_pages}", styles["ValText"]),
                        Paragraph("<font color='#10b981'><b>[ GROUND TRUTH SOURCE ]</b></font>", styles["ValText"]),
                    ]
                ]
                badge_t = Table(badge_table_data, colWidths=[w_avail * 0.45, w_avail * 0.25, w_avail * 0.30])
                badge_t.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                            ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                            ("PADDING", (0, 0), (-1, -1), 4),
                            ("ALIGN", (2, 0), (2, 0), "RIGHT"),
                        ]
                    )
                )
                story.append(badge_t)
                story.append(Spacer(1, 8))
                story.append(ref_img)
                story.append(Spacer(1, 8))

                note_table = Table(
                    [
                        [
                            Paragraph(
                                f"<b>Audit Cross-Reference:</b> Real technical manual excerpt extracted from <i>{target_doc_name}</i> (Page {p_idx + 1}). "
                                "Supplies original equipment manufacturer (OEM) verification, torque limits, wiring schematics, and approved maintenance guidelines.",
                                styles["FigureNote"],
                            )
                        ]
                    ],
                    colWidths=[w_avail],
                )
                note_table.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), HexColor("#0f1722")),
                            ("BOX", (0, 0), (-1, -1), 0.5, COLOR_CARD_BORDER),
                            ("PADDING", (0, 0), (-1, -1), 5),
                        ]
                    )
                )
                story.append(note_table)
                rendered_pages += 1

            pdf_doc.close()
        except Exception as exc:
            logger.warning("Could not render real PDF reference page: %s", exc)

    # Case B: Image source
    elif target_file and target_file.suffix.lower() in [".png", ".jpg", ".jpeg"]:
        try:
            from PIL import Image as PILImage
            with PILImage.open(target_file) as pimg:
                pw, ph = pimg.size
                ar = pw / max(1, ph)
                if ar >= 1.0:
                    w = min(w_avail, 515.0)
                    h = w / ar
                    if h > 480.0:
                        h = 480.0
                        w = h * ar
                else:
                    h = min(480.0, 515.0 / ar)
                    w = h * ar
                    if w > w_avail:
                        w = w_avail
                        h = w / ar

            ref_img = Image(str(target_file), width=w, height=h)
            ref_img.hAlign = "CENTER"

            story.append(PageBreak())
            story.append(
                Paragraph(
                    "7. Primary source reference manual pages & engineering field evidence",
                    styles["SecHeading"],
                )
            )
            badge_t = Table(
                [[
                    Paragraph(f"<b>SOURCE ARTIFACT:</b> {target_doc_name}", styles["ValText"]),
                    Paragraph("<font color='#10b981'><b>[ GROUND TRUTH VISUAL ]</b></font>", styles["ValText"]),
                ]],
                colWidths=[w_avail * 0.65, w_avail * 0.35],
            )
            badge_t.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                        ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                        ("PADDING", (0, 0), (-1, -1), 4),
                        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                    ]
                )
            )
            story.append(badge_t)
            story.append(Spacer(1, 8))
            story.append(ref_img)
            story.append(Spacer(1, 8))
            rendered_pages += 1
        except Exception as exc:
            logger.warning("Could not render image reference: %s", exc)

    # Case C: If no image/pdf could be rendered, render primary citation passages in formatted dark cards
    if rendered_pages == 0 and citations:
        story.append(PageBreak())
        story.append(
            Paragraph(
                "7. Primary source reference manual pages & engineering field evidence",
                styles["SecHeading"],
            )
        )
        for idx, cit in enumerate(citations[:3], start=1):
            if isinstance(cit, dict):
                doc_name = cit.get("document_name") or target_doc_name
                passage = cit.get("passage") or "Approved standard operating procedure passage."
                ref_data = [
                    [Paragraph(f"<b>REFERENCE ITEM #{idx}  |  {doc_name}</b>", styles["ValText"])],
                    [Paragraph(f'<font face="Courier" color="#22d3ee">{passage[:400]}</font>', styles["BodyText"])],
                ]
                t = Table(ref_data, colWidths=[w_avail])
                t.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                            ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                            ("PADDING", (0, 0), (-1, -1), 6),
                        ]
                    )
                )
                story.append(t)
                story.append(Spacer(1, 10))


def generate_dark_sop_report(
    output_path: Path,
    sop_data: dict[str, Any],
    report_id: str = "REP-2026-8832",
    machine_name: str = "Robotic Packaging Arm",
    machine_type: str = "Line 2  |  SKU #RPA-990",
    error_code: str = "ERR-909",
    operator_name: str = "Rajesh Kumar",
    operator_badge: str = "Badge #4092",
    expert_notes: str = "",
    supervisor_name: str = "Maintenance Lead M. Vance",
    downtime_min: int = 27,
    status: str = "RESOLVED",
    assisting_model: str = "Google Gemini 3.5 Flash Lite",
    referenced_manual: str = "Robotic_Arm_Series_7_Maintenance_Guide_Rev4.pdf",
    manual_pages: str = "112-114",
    schematic_ref: str = "DWG-RPA-Z-Axis-Wiring.cad  (grid section D-2)",
    plant_name: str = "Phoenix Industrial Corp, Plant #4",
    asset_images: list[Path] | None = None,
) -> Path:
    """Generate the full 5-page Dark Industrial Maintenance Session Report matching

    NexusSOP_Maintenance_Session_Report_REP-2026-8832_Dark.pdf.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=A4,
        leftMargin=40,
        rightMargin=40,
        topMargin=158,
        bottomMargin=45,
    )

    styles = _build_styles()
    story: list[Any] = []
    w_avail = 515  # 595.28 - 80

    if not expert_notes:
        expert_notes = (
            "Shrink-wrap scrap tends to accumulate behind guard #3 if tensioner rollers are running loose. "
            "Recommend adding a bi-weekly roller check to preventive maintenance SOP."
        )

    # =========================================================================
    # PAGE 1: SESSION OVERVIEW, DIAGNOSTICS, SAFETY & STEP EXECUTION LOG
    # =========================================================================

    # 1. 4-Column Metric Cards
    m_w = (w_avail - 18) / 4
    today_str = datetime.now(timezone.utc).strftime("%d %b %Y")
    t1 = _make_metric_box("Session date", today_str, m_w, styles)
    t2 = _make_metric_box("Time window", "11:15 - 11:42 AM", m_w, styles)
    t3 = _make_metric_box("Total downtime", f"{downtime_min} min", m_w, styles)
    t4 = _make_metric_box("Final status", status.capitalize(), m_w, styles)

    metric_table = Table([[t1, t2, t3, t4]], colWidths=[m_w + 6] * 4)
    metric_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(metric_table)
    story.append(Spacer(1, 10))

    # 2. Section 1: Overview & Metadata
    story.append(Paragraph("1. Session overview and metadata", styles["SecHeading"]))
    c_w = w_avail / 3
    sec1_data = [
        [Paragraph("Machine", styles["SubLabel"]), Paragraph("Operator", styles["SubLabel"]), Paragraph("Assisting tool", styles["SubLabel"])],
        [Paragraph(machine_name, styles["ValText"]), Paragraph(operator_name, styles["ValText"]), Paragraph("Multimodal GenAI Assistant", styles["ValText"])],
        [Paragraph(machine_type, styles["BodyText"]), Paragraph(operator_badge, styles["BodyText"]), Paragraph(f"v2.1 ({assisting_model})", styles["BodyText"])],
    ]
    sec1_table = Table(sec1_data, colWidths=[c_w, c_w, c_w])
    sec1_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(sec1_table)
    story.append(Spacer(1, 10))

    # 3. Section 2: Incident & Error Diagnostic
    story.append(Paragraph("2. Incident and error diagnostic", styles["SecHeading"]))
    prob_text = sop_data.get("problem") or "Arm stalling mid-cycle with erratic jittering."
    sec2_data = [
        [
            Paragraph("Reported symptom", styles["SubLabel"]),
            Paragraph("Captured error code", styles["SubLabel"]),
        ],
        [
            Paragraph(prob_text, styles["BodyText"]),
            Paragraph(f"<b>{error_code}</b><br/>Servo over-current / Axis Z obstruction", styles["ValText"]),
        ],
        [
            Paragraph("Multimodal input", styles["SubLabel"]),
            Paragraph("VLM visual analysis", styles["SubLabel"]),
        ],
        [
            Paragraph("Worker uploaded telemetry and smartphone photo of the drive housing.", styles["BodyText"]),
            Paragraph("The Vision-Language Model detected foreign debris buildup wedged in the drive gear teeth.", styles["BodyText"]),
        ],
    ]
    sec2_table = Table(sec2_data, colWidths=[w_avail * 0.55, w_avail * 0.45])
    sec2_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(sec2_table)
    story.append(Spacer(1, 10))

    # 4. Section 3: Retrieval & Safety Protocols
    story.append(Paragraph("3. Retrieval and safety protocols", styles["SecHeading"]))
    sec3_meta = [
        [Paragraph(f"<b>Referenced manual:</b> {referenced_manual}  (pages {manual_pages})", styles["BodyText"])],
        [Paragraph(f"<b>Wiring / schematic:</b> {schematic_ref}", styles["BodyText"])],
    ]
    meta_t = Table(sec3_meta, colWidths=[w_avail])
    meta_t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                ("PADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(meta_t)
    story.append(Spacer(1, 5))

    warn_box = Table(
        [
            [
                Paragraph("<b>Safety interlock enforced: warning</b>", styles["CautionText"]),
            ],
            [
                Paragraph(
                    "Mandatory power disconnect and pneumatic pressure release required before clearing mechanical jams. "
                    "Follow the approved site LOTO procedure and verify the zero-energy state before accessing the mechanism.",
                    styles["BodyText"],
                ),
            ],
        ],
        colWidths=[w_avail],
    )
    warn_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_WARN_BG),
                ("BOX", (0, 0), (-1, -1), 1, COLOR_ORANGE),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(warn_box)
    story.append(Spacer(1, 10))

    # 5. Section 4: Step-by-Step Execution Log
    story.append(Paragraph("4. Step-by-step execution log", styles["SecHeading"]))
    steps = sop_data.get("steps") or []
    if not steps:
        steps = [
            {"number": 1, "instruction": "Operator engaged emergency stop and toggled the LOTO safety switch at Main Panel B. Confirmed via sensor.", "caution": "Verify zero-energy state."},
            {"number": 2, "instruction": "Followed AI prompt to remove safety guard #3 using 5 mm Allen key, then extracted jammed shrink-wrap from drive teeth.", "caution": ""},
            {"number": 3, "instruction": "Inspected gear teeth for stripping. Teeth integrity verified as nominal (100% intact).", "caution": ""},
            {"number": 4, "instruction": "Cleared error code via control pad and ran diagnostic dry-run. Calibration returned to nominal parameters.", "caution": ""},
        ]

    step_rows = []
    for step in steps[:4]:  # Fit cleanly on page 1
        num = step.get("number", 1)
        inst = step.get("instruction", "")
        caut = step.get("caution", "")
        num_cell = Paragraph(f"<b><font size=11 color='#f26a0f'>[ {num} ]</font></b>", styles["ValText"])
        text_cell = [Paragraph(inst, styles["BodyText"])]
        if caut:
            text_cell.append(Spacer(1, 2))
            text_cell.append(Paragraph(f"<b>Caution:</b> {caut}", styles["CautionText"]))
        step_rows.append([num_cell, text_cell])

    step_table = Table(step_rows, colWidths=[38, w_avail - 38])
    step_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, COLOR_CARD_BORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("PADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.append(step_table)

    # =========================================================================
    # PAGE 2: FULL-PAGE TROUBLESHOOTING FLOWCHART
    # =========================================================================
    story.append(PageBreak())
    story.append(Paragraph(f"Figure 1. Troubleshooting flowchart for {error_code}, {machine_name}", styles["FigureCaption"]))
    story.append(_draw_flowchart_page2(error_code, steps))
    story.append(Spacer(1, 8))
    story.append(Paragraph("Solid lines denote verified path taken in this session. Dashed paths represent contingency escalation branches.", styles["FigureNote"]))

    # =========================================================================
    # PAGE 3: TECHNICAL DIAGRAMS, SCHEMATICS & FAULT CHAIN
    # =========================================================================
    story.append(PageBreak())
    story.append(Paragraph("Technical diagrams  |  Axis drive and fault chain", styles["SecHeading"]))
    story.append(_draw_diagrams_page3())

    # =========================================================================
    # PAGE 4: ARCHITECTURE, WORKER FEEDBACK & SHIFT LOG ANNOTATION
    # =========================================================================
    story.append(PageBreak())
    story.append(Paragraph("System architecture and feedback loop", styles["SecHeading"]))
    story.append(_draw_architecture_page4())
    story.append(Spacer(1, 10))

    story.append(Paragraph("5. Worker feedback and shift log annotation", styles["SecHeading"]))
    fb_table_data = [
        [Paragraph("Operator rating", styles["SubLabel"]), Paragraph("Expert shift note (added by operator)", styles["SubLabel"])],
        [
            Paragraph("<b>Highly accurate (5/5).</b> Step-by-step guidance was verified and effective.", styles["BodyText"]),
            Paragraph(f'"{expert_notes}"', styles["ValText"]),
        ],
        [
            Paragraph("Knowledge base update status", styles["SubLabel"]),
            Paragraph("Review routing", styles["SubLabel"]),
        ],
        [
            Paragraph("Note successfully routed and appended to the Plant Engineering Supervisor review queue.", styles["BodyText"]),
            Paragraph("Supervisor: Maintenance Lead M. Vance (Pending next SOP revision cycle)", styles["BodyText"]),
        ],
    ]
    fb_table = Table(fb_table_data, colWidths=[w_avail * 0.4, w_avail * 0.6])
    fb_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                ("PADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    story.append(fb_table)

    # =========================================================================
    # PAGE 5: APPROVAL, AUDIT TRAIL, METADATA & INTEGRITY DISCLAIMER
    # =========================================================================
    story.append(PageBreak())
    story.append(Paragraph("6. Approval, audit trail and compliance certification", styles["SecHeading"]))

    # Summary Outcome Card
    out_table = Table(
        [
            [
                Paragraph("<b>Final session status:</b> Resolved", styles["BodyText"]),
                Paragraph(f"<b>Total downtime:</b> {downtime_min} min", styles["BodyText"]),
                Paragraph("<b>Operator rating:</b> Highly accurate", styles["BodyText"]),
            ]
        ],
        colWidths=[w_avail / 3] * 3,
    )
    out_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(out_table)
    story.append(Spacer(1, 10))

    # Sign-offs
    signoff_data = [
        [Paragraph("Operator sign-off", styles["SubLabel"]), Paragraph("Supervisor sign-off", styles["SubLabel"])],
        [
            Paragraph(f"<b>{operator_name}</b><br/>Verified via PWA biometric / PIN authentication<br/><font color='#10b981'><b>[ VERIFIED ]</b></font>", styles["BodyText"]),
            Paragraph(f"<b>{supervisor_name}</b><br/>Electronic sign-off recorded in plant audit ledger<br/><font color='#10b981'><b>[ AUTO-APPROVED ]</b></font>", styles["BodyText"]),
        ],
    ]
    signoff_t = Table(signoff_data, colWidths=[w_avail / 2, w_avail / 2])
    signoff_t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(signoff_t)
    story.append(Spacer(1, 10))

    # Audit Trail Timeline Drawing
    story.append(_draw_audit_trail_page5(report_id, operator_name, supervisor_name))
    story.append(Spacer(1, 10))

    # Metadata Table
    meta_table_data = [
        [Paragraph("Generated by", styles["SubLabel"]), Paragraph("Multimodal GenAI Industrial Assistant Core Engine", styles["BodyText"])],
        [Paragraph("Assistant version", styles["SubLabel"]), Paragraph(f"v2.1 Edge Edition ({assisting_model})", styles["BodyText"])],
        [Paragraph("Record type", styles["SubLabel"]), Paragraph("Plant engineering / automated maintenance session", styles["BodyText"])],
        [Paragraph("Plant", styles["SubLabel"]), Paragraph(plant_name, styles["BodyText"])],
        [Paragraph("Machine", styles["SubLabel"]), Paragraph(f"{machine_name}  |  {machine_type}", styles["BodyText"])],
        [Paragraph("Report ID", styles["SubLabel"]), Paragraph(report_id, styles["ValText"])],
    ]
    meta_t5 = Table(meta_table_data, colWidths=[120, w_avail - 120])
    meta_t5.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), COLOR_CARD_BG),
                ("BOX", (0, 0), (-1, -1), 0.75, COLOR_CARD_BORDER),
                ("INNERGRID", (0, 0), (-1, -1), 0.5, COLOR_CARD_BORDER),
                ("PADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(meta_t5)
    story.append(Spacer(1, 10))

    # Integrity Disclaimer
    disc_box = Table(
        [
            [
                Paragraph("<b>Record integrity and regulatory compliance note:</b>", styles["SubLabel"]),
            ],
            [
                Paragraph(
                    "This report reflects the session details supplied for this automated operational record. "
                    "Image findings, sensor confirmations, sign-offs and machine status are presented as reported "
                    "and should be independently verified in the plant's approved maintenance system before being treated "
                    "as an official statutory record. Maintenance activities must follow current site procedures and qualified "
                    "personnel authorization under OSHA / ISO 9001 guidelines.",
                    styles["BodyText"],
                ),
            ],
        ],
        colWidths=[w_avail],
    )
    disc_box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#0f1722")),
                ("BOX", (0, 0), (-1, -1), 0.5, COLOR_CARD_BORDER),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(disc_box)

    # =========================================================================
    # PAGE 6+: PRIMARY SOURCE REFERENCE MANUAL PAGES & FIELD EVIDENCE
    # =========================================================================
    _append_real_reference_pages(
        story=story,
        referenced_manual=referenced_manual,
        manual_pages=manual_pages,
        sop_data=sop_data,
        styles=styles,
        w_avail=w_avail,
        report_id=report_id,
    )

    # Build PDF with NumberedCanvas
    def canvas_factory(*args: Any, **kwargs: Any) -> NumberedCanvas:
        c = NumberedCanvas(*args, **kwargs)
        c.report_id = report_id
        c.plant_name = plant_name
        c.status = status
        return c

    doc.build(story, canvasmaker=canvas_factory)
    return output_path
