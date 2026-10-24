# Hackathon Pitch

## 60-Second Pitch

Industrial teams lose time searching long manuals, while an unsupported answer can create real safety risk. NexusSOP turns approved source documents into searchable evidence: every grounded response shows the passage it came from, low-evidence questions are refused, and operational requests are routed away from generated instructions. New documents stay out of retrieval until a reviewer approves them. Supervisors can see what was answered, what was abstained, and what was escalated.

Our prototype demonstrates the full workflow locally with a FastAPI service, persistent SQLite records, document upload and review, source citations, and live analytics. An optional language model can summarize retrieved evidence for non-operational questions; the default still works offline with extractive retrieval. The important design choice is that the assistant is not an authority for operating machinery.

## Live Demo Sequence

1. Show a safe evidence question and open its cited paragraph.
2. Ask for PLC repair instructions and show the refusal.
3. Upload and approve a short text source, then retrieve its content.
4. Log an escalation and show the live dashboard and CSV export.

## Honest Evaluation

The prototype demonstrates traceable retrieval and governance, not measured downtime reduction or production safety outcomes. The starter documents are synthetic. The next validation step is evaluation with authorized plant documents, domain-expert review, retrieval and refusal test sets, and an authenticated reviewer workflow.