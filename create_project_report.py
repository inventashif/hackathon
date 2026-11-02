from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle,
    KeepTogether
)


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output" / "NexusSOP_Project_Analysis_and_Judging_Guide.pdf"
OUT.parent.mkdir(parents=True, exist_ok=True)

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(
    name="CoverTitle", parent=styles["Title"], fontName="Helvetica-Bold",
    fontSize=26, leading=31, alignment=TA_CENTER, textColor=colors.HexColor("#102a43"),
    spaceAfter=18,
))
styles.add(ParagraphStyle(
    name="CoverSub", parent=styles["Normal"], fontSize=13, leading=18,
    alignment=TA_CENTER, textColor=colors.HexColor("#486581"),
))
styles.add(ParagraphStyle(
    name="Section", parent=styles["Heading1"], fontName="Helvetica-Bold",
    fontSize=17, leading=21, textColor=colors.HexColor("#102a43"),
    spaceBefore=12, spaceAfter=9,
))
styles.add(ParagraphStyle(
    name="Subsection", parent=styles["Heading2"], fontName="Helvetica-Bold",
    fontSize=12, leading=15, textColor=colors.HexColor("#243b53"),
    spaceBefore=8, spaceAfter=5,
))
styles.add(ParagraphStyle(
    name="Body2", parent=styles["BodyText"], fontSize=9.5, leading=14,
    textColor=colors.HexColor("#243b53"), spaceAfter=6,
))
styles.add(ParagraphStyle(
    name="Small", parent=styles["BodyText"], fontSize=8, leading=11,
    textColor=colors.HexColor("#486581"),
))
styles.add(ParagraphStyle(
    name="Callout", parent=styles["BodyText"], fontSize=11, leading=16,
    textColor=colors.HexColor("#102a43"), leftIndent=10, rightIndent=10,
    spaceBefore=5, spaceAfter=8,
))


def p(text, style="Body2"):
    return Paragraph(text, styles[style])


def bullets(items):
    return [p(f"• {item}") for item in items]


def table(rows, widths, header=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bcccdc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]
    if header:
        commands += [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#d9eaf7")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#102a43")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]
    t.setStyle(TableStyle(commands))
    return t


def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#bcccdc"))
    canvas.line(40,  thirty := 32, A4[0] - 40, thirty)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#627d98"))
    canvas.drawString(40, 19, "NexusSOP | Project analysis and presentation guide")
    canvas.drawRightString(A4[0] - 40, 19, f"Page {doc.page}")
    canvas.restoreState()


story = []

# Cover
story += [
    Spacer(1, 1.0 * inch),
    p("NexusSOP", "CoverTitle"),
    p("Project Analysis, Completion Status and Judge Presentation Guide", "CoverSub"),
    Spacer(1, 20),
    p("<b>Problem Statement 2:</b> Multimodal GenAI Assistant for Industrial SOPs, Manuals and Expert Knowledge", "CoverSub"),
    Spacer(1, 35),
    p("Prepared from the recent project review and implementation discussions", "Small"),
    Spacer(1, 8),
    p("Current validation: <b>87 automated tests passed</b>", "Small"),
    PageBreak(),
]

# Executive summary
story += [
    p("1. Executive Summary", "Section"),
    p(
        "NexusSOP is a safety-aware industrial knowledge assistant. It searches approved manuals and SOPs, "
        "returns evidence-backed answers, generates structured procedures, displays original figures, supports "
        "voice interaction, preserves engineer feedback, and refuses to guess when evidence is insufficient.",
    ),
    p(
        "<b>Estimated hackathon prototype completion:</b> 75–85%. The core product is implemented. "
        "The remaining work is concentrated in deeper visual reasoning, evaluation metrics, cloud deployment "
        "demonstration, and final presentation polish.",
    ),
    p(
        "<b>Core positioning:</b> NexusSOP is not a generic chatbot over PDFs. It is a governed industrial "
        "knowledge workflow with approval, citations, abstention, escalation and human review.",
        "Callout",
    ),
    p("Recommended one-line pitch:", "Subsection"),
    p(
        "<b>“NexusSOP transforms approved industrial manuals and expert knowledge into traceable, "
        "voice-accessible SOPs while refusing to hallucinate unsafe instructions.”</b>",
        "Callout",
    ),
]

# Problem alignment
story += [
    p("2. Alignment with Problem Statement 2", "Section"),
    table([
        [p("Requirement", "Small"), p("Status", "Small"), p("Evidence in project", "Small")],
        [p("GenAI assistant"), p("Implemented"), p("Optional NVIDIA/Gemini model with local fallback.")],
        [p("RAG over manuals/SOPs"), p("Implemented"), p("Approved-document retrieval, relevance scoring and citations.")],
        [p("Text documents"), p("Implemented"), p("PDF, DOCX, TXT and Markdown ingestion.")],
        [p("Images and diagrams"), p("Partially implemented"), p("OCR, embedded image extraction, page snapshots and original figure export.")],
        [p("Grounded answers"), p("Strong"), p("Retrieved references are passed as numbered evidence and citations are validated.")],
        [p("Troubleshooting/SOP guidance"), p("Implemented"), p("Structured steps, cautions, checks, figures and PDF export.")],
        [p("Chat interface"), p("Implemented"), p("Browser chat with persistent sessions.")],
        [p("Voice interface"), p("Implemented foundation"), p("Gemini Live relay and server-side knowledge tool.")],
        [p("Expert knowledge"), p("Implemented"), p("Engineer feedback, notes, shared rooms and synthesized notes.")],
        [p("Analytics"), p("Implemented"), p("Dashboard, query outcomes, escalations and CSV export.")],
        [p("Cloud scalability"), p("Partially implemented"), p("Docker and modular FastAPI architecture; current default is local SQLite.")],
    ], [1.65*inch, 1.25*inch, 3.9*inch]),
]

# Completed
story += [
    p("3. Completed Features", "Section"),
    *bullets([
        "Multi-user authentication with admin and engineer roles.",
        "Session cookies, password hashing, invitations and user management.",
        "Document upload with human approval before retrieval.",
        "PDF, DOCX, TXT, Markdown and image ingestion.",
        "OCR preprocessing for image uploads.",
        "PDF page markers and embedded image/figure extraction.",
        "Lexical RAG with stemming, IDF relevance and query-term coverage.",
        "Retrieved evidence passed to the model as numbered references.",
        "Citation validation so unsupported source IDs are removed.",
        "Low-evidence abstention and safety-oriented operational refusal.",
        "Chat, shared rooms, persistent text sessions and collaborative polling.",
        "Voice relay with server-side knowledge retrieval.",
        "SOP generation with steps, cautions, checks, citations and figures.",
        "Engineer feedback, manual notes and synthesized notes.",
        "Executive PDF report generation with source reference pages.",
        "New compact black-and-white Quick B/W SOP export.",
        "Analytics dashboard, escalation tickets and CSV export.",
        "Docker setup and automated test coverage.",
    ]),
]

# Recent enhancement
story += [
    p("4. Recent Enhancement: Quick B/W SOP", "Section"),
    p(
        "A separate compact export mode was added without removing the existing executive report. "
        "This directly supports the request for a simple black-and-white sheet containing less text, "
        "important content, diagrams and original-page references.",
    ),
    table([
        [p("Feature", "Small"), p("Implementation", "Small")],
        [p("Black-and-white layout"), p("A new compact ReportLab PDF renderer uses a clean monochrome layout.")],
        [p("Less text"), p("Step descriptions and warnings are shortened to essential content.")],
        [p("Important information"), p("Essential steps, cautions, warnings and verification checks are separated.")],
        [p("Original diagrams"), p("Only source-linked extracted figures are included; no invented diagrams are used.")],
        [p("Original page references"), p("Citation IDs are resolved to page markers such as “p. 12”.")],
        [p("UI access"), p("A “Quick B/W SOP” button appears beside the executive PDF export.")],
        [p("Endpoint"), p("Authenticated GET/POST endpoint: /api/sops/{id}/quick-export")],
    ], [2.0*inch, 4.8*inch]),
    p(
        "<b>Judge value:</b> The executive report is useful for supervisors and audit review, while the Quick B/W "
        "SOP is useful for a technician who needs a fast, printable reference.",
        "Callout",
    ),
]

# Pending
story += [
    p("5. Pending and Partially Complete Work", "Section"),
    table([
        [p("Priority", "Small"), p("Pending item", "Small"), p("Why it matters", "Small")],
        [p("High"), p("True diagram understanding"), p("The assistant should reason about labels, symbols, arrows, tables and schematics, not only extract/display images.")],
        [p("High"), p("Polished voice-to-SOP scenario"), p("Show voice symptom/error input, clarification, retrieval, SOP generation and display as one complete flow.")],
        [p("High"), p("Evaluation benchmark"), p("Measure retrieval hit rate, citation precision, abstention accuracy, latency and escalation rate.")],
        [p("High"), p("Cloud deployment story"), p("Demonstrate Docker/cloud architecture, object storage and a database separation plan.")],
        [p("Medium"), p("Multilingual support"), p("Hindi or another regional-language mode could make the solution more memorable and inclusive.")],
        [p("Medium"), p("Production security"), p("Encryption at rest, malware scanning, secure secrets, audit retention and removal of default credentials.")],
        [p("Medium"), p("Industrial integrations"), p("CMMS/ERP/maintenance ticket connectors and external alert delivery.")],
        [p("Medium"), p("Advanced analytics"), p("Time saved, common faults, unanswered questions, manual usage and escalation trends.")],
        [p("Roadmap"), p("Expert safety validation"), p("Domain-reviewed test sets and formal approval before real equipment use.")],
    ], [0.7*inch, 2.0*inch, 4.1*inch]),
    p(
        "<b>Important:</b> These items are not all required for the hackathon prototype. "
        "For judging, prioritize the first four high-impact items and present the rest as a production roadmap.",
    ),
]

# Judge appeal
story += [
    p("6. Features Most Likely to Attract Judges", "Section"),
    *bullets([
        "<b>Evidence-first answers:</b> Every answer points back to exact approved source material.",
        "<b>Safety-aware abstention:</b> The assistant refuses unsupported or dangerous machine-operation requests.",
        "<b>Human-in-the-loop governance:</b> New documents require approval before entering retrieval.",
        "<b>Multimodal foundation:</b> Manuals, images, OCR, figures and voice are included in one workflow.",
        "<b>Automatic SOP generation:</b> Evidence becomes structured steps, cautions, checks and printable outputs.",
        "<b>Expert knowledge preservation:</b> Feedback and field notes remain attached to the generated SOP.",
        "<b>Two audience outputs:</b> Detailed executive report for supervisors and compact B/W sheet for technicians.",
        "<b>Offline fallback:</b> Local extractive RAG keeps the demo usable even without an external model key.",
        "<b>Analytics:</b> Supervisors can see recurring issues, outcomes and escalations.",
        "<b>Scalable foundation:</b> FastAPI, Docker and modular ingestion provide a clear path to cloud deployment.",
    ]),
]

# Demo
story += [
    p("7. Recommended Live Demonstration", "Section"),
    p("Use one complete industrial story instead of showing disconnected features:", "Body2"),
    table([
        [p("Step", "Small"), p("What to show", "Small"), p("Judge takeaway", "Small")],
        [p("1"), p("Upload a machine manual and show Pending Review."), p("Knowledge is governed, not blindly indexed.")],
        [p("2"), p("Approve the manual as administrator."), p("Human review controls source quality.")],
        [p("3"), p("Ask a grounded question and open the cited page."), p("The answer is traceable.")],
        [p("4"), p("Ask a question absent from the manual."), p("The assistant abstains rather than inventing.")],
        [p("5"), p("Ask a risky operational question."), p("Safety boundary and escalation are visible.")],
        [p("6"), p("Use voice to provide machine, symptom and error code."), p("Factory-floor interaction is practical.")],
        [p("7"), p("Generate an SOP with figures and checks."), p("RAG becomes a usable work artifact.")],
        [p("8"), p("Download Quick B/W SOP."), p("Technicians get a concise printable reference.")],
        [p("9"), p("Add engineer feedback and show analytics."), p("Expert knowledge compounds over time.")],
    ], [0.45*inch, 3.55*inch, 2.8*inch]),
]

# Talking points
story += [
    p("8. Talking Points for Judges", "Section"),
    p("<b>Problem:</b> Industrial teams lose time searching long manuals, while unsupported AI instructions can create safety risk.", "Body2"),
    p("<b>Solution:</b> NexusSOP retrieves only approved evidence, shows the source, generates concise procedures and knows when to stop.", "Body2"),
    p("<b>Innovation:</b> The project combines RAG, governance, voice, figures, feedback and analytics rather than presenting only a chat window.", "Body2"),
    p("<b>Impact:</b> It can reduce manual search time, preserve retiring experts’ knowledge and route uncertain cases to qualified personnel.", "Body2"),
    p("<b>Trust:</b> The system’s most important behavior is not just answering; it is refusing unsupported answers and exposing its evidence.", "Body2"),
    p("<b>Scalability:</b> Additional machines, manuals, plants, languages and maintenance systems can be onboarded through the modular architecture.", "Body2"),
    p("Suggested judge-facing answer when asked about limitations:", "Subsection"),
    p(
        "“This is a hackathon prototype, not a certified plant-control system. We deliberately show that limitation. "
        "The next validation stage is domain-expert evaluation with authorized manuals, measurable retrieval and refusal tests, "
        "secure cloud storage and approved maintenance-system integration.”",
        "Callout",
    ),
]

# Technical architecture
story += [
    p("9. Technical Architecture Summary", "Section"),
    *bullets([
        "Browser interface: industrial chat, dashboard, document review, SOP cards and voice pages.",
        "FastAPI backend: authentication, document APIs, chat, voice relay, SOP APIs and analytics.",
        "Ingestion layer: PyMuPDF, pypdf, python-docx, image normalization and Tesseract OCR.",
        "Retrieval layer: Porter stemming, lexical scoring, IDF relevance, evidence coverage and source excerpts.",
        "Generation layer: optional NVIDIA/Gemini models with extractive fallback.",
        "Governance layer: role-based access, approval gates, citation filtering, abstention and escalation.",
        "Persistence: SQLite records for users, sessions, rooms, documents, assets, SOPs, feedback, notes and analytics.",
        "Export layer: executive dark report and compact black-and-white source-referenced SOP.",
    ]),
]

# Validation
story += [
    p("10. Validation and Current Confidence", "Section"),
    table([
        [p("Check", "Small"), p("Result", "Small")],
        [p("Automated test suite"), p("87 passed, 1 non-blocking fitz deprecation warning.")],
        [p("Python compilation"), p("Passed after compact export changes.")],
        [p("Patch formatting"), p("git diff --check passed.")],
        [p("Compact export integration test"), p("Passed; PDF contains essential steps and source-page references.")],
        [p("Existing executive export"), p("Preserved and regression-tested.")],
    ], [2.4*inch, 4.4*inch]),
    p(
        "The project is suitable for a strong hackathon demonstration. Avoid claiming measured factory savings "
        "until authorized plant data and expert-reviewed evaluation are available.",
    ),
]

# Final
story += [
    p("11. Final Presentation Checklist", "Section"),
    *bullets([
        "Prepare one approved manual and one realistic machine/error scenario.",
        "Verify all demo API keys, login credentials and uploaded documents before judging.",
        "Do not use the default admin password in the final presentation.",
        "Keep the cited original manual page ready to open after every key answer.",
        "Show both executive PDF and Quick B/W SOP exports.",
        "Demonstrate one successful grounded answer and one safe abstention.",
        "Explain current limitations honestly and connect them to the roadmap.",
        "End with the safety-aware positioning statement, not with a list of technologies.",
    ]),
    Spacer(1, 12),
    p(
        "<b>Closing statement:</b> NexusSOP makes industrial knowledge usable, traceable and safer by connecting "
        "approved manuals, expert review, multimodal interaction and concise field-ready SOPs.",
        "Callout",
    ),
]

doc = SimpleDocTemplate(
    str(OUT), pagesize=A4, rightMargin=40, leftMargin=40,
    topMargin=42, bottomMargin=42, title="NexusSOP Project Analysis and Judging Guide",
    author="NexusSOP Team",
)
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print(OUT)
