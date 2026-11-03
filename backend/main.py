from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import asyncio
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx
import websockets
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect, Request, Response, Cookie, Depends
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from pypdf import PdfReader
from dotenv import load_dotenv

from backend.stemmer import stem as _stemmer
from backend import auth, detective, ingest, sop


logger = logging.getLogger("nexus_sop")
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
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.5-flash-lite")
GEMINI_API_BASE_URL = os.environ.get(
    "GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
).rstrip("/")
GEMINI_LIVE_MODEL = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.8-live")
MAX_UPLOAD_BYTES = int(os.environ["MAX_UPLOAD_BYTES"]) if os.environ.get("MAX_UPLOAD_BYTES") else None
MIN_RETRIEVAL_SCORE = 0.12
MIN_EVIDENCE_COVERAGE = 0.5
CONVERSATIONAL_REPLY = (
    "Hello! I'm the MechTech AI evidence assistant. Ask me about document review, "
    "evidence policy, or the system guide, and I'll answer with cited source excerpts."
)

IMAGE_UPLOAD_DIR = ROOT / "data" / "uploads"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}

DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".md", ".docx"} | IMAGE_EXTENSIONS


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
    "s", "t", "d", "ll", "m", "re", "ve",
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
    r"steps?\s+(?:to|for|of|do|by\s+step|wise)|procedure\s+(?:to|for|of)|"
    r"give\s+me|show\s+me|walk\s+me|teach\s+me|instructions?\s+(?:for|to|on)|"
    r"what(?:'s|\s+is)\s+the\s+(?:procedure|process|steps?|solution|fix)|"
    r"solution\s+(?:for|to|in)|step\s*(?:by\s*step|wise)|summariz\w*|analys\w*|analyz\w*)\b",
    re.IGNORECASE,
)

# Words that turn a lookup question into a request for procedures.
PROCEDURE_REQUEST = re.compile(
    r"\b(steps?|procedure|instructions?|process|work\s*sheet|task\s*list|solutions?|sop\w*|guide|workflow|walkthrough|action\s*plan|troubleshoot\w*|remediation|fix\w*)\b",
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


SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "english": "English",
    "hi": "Hindi",
    "hindi": "Hindi",
    "es": "Spanish",
    "spanish": "Spanish",
    "de": "German",
    "german": "German",
    "fr": "French",
    "french": "French",
    "ja": "Japanese",
    "japanese": "Japanese",
}


def normalize_language(lang: str | None) -> str:
    """Normalize language code or string to canonical title (e.g. 'Hindi', 'English')."""
    if not lang:
        return "English"
    cleaned = str(lang).strip().lower()
    return SUPPORTED_LANGUAGES.get(cleaned, lang.strip().capitalize() or "English")


CLARIFICATION_PROMPTS: dict[str, str] = {
    "English": (
        "To help diagnose this issue accurately from approved manuals, please provide specific observations and evidence:\n\n"
        "1. **Machine & Model Identification:** Which exact machine and model number are you working on? (e.g., Siemens SINAMICS G120C, Fagor CNC 8055)\n"
        "2. **Sensory Observations:**\n"
        "   - What abnormal noises do you hear? (e.g., screeching, humming, knocking, grinding)\n"
        "   - Do you observe excessive vibration, unusual heat buildup, smoke, or fluid leaks?\n"
        "3. **Operational Context:** What was the equipment doing right when the anomaly occurred (e.g., accelerating, continuous running under peak load, cold starting, or idling)?\n"
        "4. **Control Panel / HMI Evidence:** What exact fault/alarm code, message, or indicator LED pattern is shown on the display? (e.g., F30001, Alarm 2000)"
    ),
    "Hindi": (
        "स्वीकृत मैन्युअल के आधार पर इस समस्या का सटीक निदान करने के लिए, कृपया कुछ विशिष्ट अवलोकन और साक्ष्य प्रदान करें:\n\n"
        "1. **मशीन और मॉडल पहचान:** आप किस विशिष्ट मशीन और मॉडल पर काम कर रहे हैं? (उदा. Siemens SINAMICS G120C, Fagor CNC 8055)\n"
        "2. **भौतिक अवलोकन (Sensory Observations):**\n"
        "   - क्या कोई असामान्य आवाज़ (खटखटाना, सीटी, घर्षण) सुनाई दे रही है?\n"
        "   - क्या अत्यधिक कंपन (vibration), असामान्य गर्मी, धुआं या तरल रिसाव (leak) दिखाई दे रहा है?\n"
        "3. **परिचालन स्थिति (Operational Context):** समस्या शुरू होने से ठीक पहले मशीन क्या कर रही थी (त्वरण/acceleration, सामान्य लोड, या स्थिर/idle)?\n"
        "4. **कंट्रोल पैनल साक्ष्य (Panel Evidence):** डिस्प्ले या HMI पर क्या विशिष्ट फॉल्ट/अलार्म कोड (उदा. F30001, Error 2000) या चेतावनी लाइट प्रदर्शित है?"
    ),
    "Spanish": (
        "Para diagnosticar este problema con precisión a partir de los manuales aprobados, proporcione observaciones y evidencias específicas:\n\n"
        "1. **Identificación de máquina y modelo:** ¿En qué máquina y modelo específico está trabajando? (ej. Siemens SINAMICS G120C, Fagor CNC 8055)\n"
        "2. **Observaciones sensoriales:**\n"
        "   - ¿Qué ruidos anormales escucha? (ej. rechinidos, zumbidos, golpeteos)\n"
        "   - ¿Detecta vibración excesiva, calor inusual, humo o fuga de fluidos?\n"
        "3. **Contexto operativo:** ¿Qué estaba haciendo el equipo justo cuando ocurrió la falla (acelerando, a plena carga o en ralentí)?\n"
        "4. **Evidencia del panel de control:** ¿Qué código de falla, mensaje de alarma o luces de advertencia se muestran en el HMI o pantalla? (ej. F30001, Error 2000)"
    ),
    "German": (
        "Um dieses Problem anhand der freigegebenen Handbücher genau zu diagnostizieren, geben Sie bitte spezifische Beobachtungen und Nachweise an:\n\n"
        "1. **Maschinen- & Modellidentifikation:** An welcher genauen Maschine und welchem Modell arbeiten Sie? (z. B. Siemens SINAMICS G120C, Fagor CNC 8055)\n"
        "2. **Sensorische Beobachtungen:**\n"
        "   - Welche ungewöhnlichen Geräusche stellen Sie fest? (z. B. Schleifen, Summen, Klopfen)\n"
        "   - Gibt es übermäßige Vibrationen, Überhitzung, Rauch oder Flüssigkeitsaustritt?\n"
        "3. **Betriebszustand:** Was tat die Maschine unmittelbar vor dem Fehler (z. B. Hochlauf, Volllast oder Leerlauf)?\n"
        "4. **Nachweise auf dem Bedienfeld:** Welcher genaue Fehlercode, Alarm oder welche LED-Anzeige wird auf dem Display/HMI angezeigt? (z. B. F30001, Fehler 2000)"
    ),
    "French": (
        "Pour diagnostiquer précisément ce problème à partir des manuels approuvés, veuillez fournir des observations et preuves spécifiques :\n\n"
        "1. **Identification de la machine et du modèle :** Sur quelle machine et quel modèle précis travaillez-vous ? (ex. Siemens SINAMICS G120C, Fagor CNC 8055)\n"
        "2. **Observations sensorielles :**\n"
        "   - Quels bruits anormaux entendez-vous ? (ex. grincements, bourdonnements, cliquetis)\n"
        "   - Constatez-vous des vibrations excessives, une surchauffe, de la fumée ou une fuite de liquide ?\n"
        "3. **Contexte opérationnel :** Que faisait l'équipement juste avant l'incident (accélération, pleine charge ou veille) ?\n"
        "4. **Preuves du panneau de contrôle :** Quel code de défaut, message d'alarme ou voyant s'affiche sur l'IHM ou l'écran ? (ex. F30001, Erreur 2000)"
    ),
    "Japanese": (
        "承認されたマニュアルに基づいてこの問題を正確に診断するために、具体的な観察事項と証拠を提供してください：\n\n"
        "1. **機械とモデルの特定:** 対象の機械名と型番・モデルは何ですか？ (例: Siemens SINAMICS G120C, Fagor CNC 8055)\n"
        "2. **感覚的な観察事項:**\n"
        "   - どのような異音（きしみ音、うなり音、打撃音）が聞こえますか？\n"
        "   - 過度な振動、異常な発熱、煙、または油・液漏れはありますか？\n"
        "3. **動作状況:** 異常が発生した直前、機械はどのような動作をしていましたか？ (加速中、全負荷運転中、アイドリング中など)\n"
        "4. **操作パネルの表示・証拠:** HMIまたは操作パネルに表示されている正確なエラーコードやアラームメッセージは何ですか？ (例: F30001, Error 2000)"
    ),
}

CONVERSATIONAL_REPLIES: dict[str, str] = {
    "English": "Hello! I am NexusSOP, your industrial SOP and knowledge assistant. How can I help you today?",
    "Hindi": "नमस्ते! मैं NexusSOP हूँ, आपका औद्योगिक SOP और ज्ञान सहायक। आज मैं आपकी क्या सहायता कर सकता हूँ?",
    "Spanish": "¡Hola! Soy NexusSOP, su asistente industrial de conocimiento y SOP. ¿En qué puedo ayudarle hoy?",
    "German": "Hallo! Ich bin NexusSOP, Ihr industrieller SOP- und Wissensassistent. Wie kann ich Ihnen heute helfen?",
    "French": "Bonjour ! Je suis NexusSOP, votre assistant industriel de SOP et de connaissances. Comment puis-je vous aider aujourd'hui ?",
    "Japanese": "こんにちは！NexusSOPです。産業用SOPおよびナレッジアシスタントです。本日はどのようなご用件でしょうか？",
}

INSUFFICIENT_EVIDENCE_REPLIES: dict[str, str] = {
    "English": "I couldn't find enough evidence in the approved knowledge base to answer that. Upload a relevant source, have it reviewed, then ask again.",
    "Hindi": "मुझे इसका उत्तर देने के लिए स्वीकृत ज्ञानकोष में पर्याप्त साक्ष्य नहीं मिले। कृपया संबंधित स्रोत अपलोड करें, उसकी समीक्षा करवाएं, और फिर पूछें।",
    "Spanish": "No pude encontrar suficiente evidencia en la base de conocimientos aprobada para responder eso. Cargue una fuente relevante, haga que la revisen y vuelva a preguntar.",
    "German": "In der freigegebenen Wissensdatenbank konnten nicht genügend Nachweise gefunden werden, um dies zu beantworten. Bitte laden Sie ein relevantes Dokument hoch, lassen Sie es prüfen und fragen Sie erneut.",
    "French": "Je n'ai pas trouvé suffisamment de preuves dans la base de connaissances approuvée pour répondre. Veuillez télécharger un document pertinent, le faire valider et réessayer.",
    "Japanese": "承認されたナレッジベース内に回答に十分な証拠が見つかりませんでした。関連するマニュアルをアップロードし、承認後に再度質問してください。",
}

OPERATIONAL_REFUSAL_REPLIES: dict[str, str] = {
    "English": "I can't provide equipment operating, repair, isolation, or troubleshooting instructions. Stop and consult the current approved site SOP and a qualified supervisor.",
    "Hindi": "मैं उपकरण संचालन, मरम्मत, अलगाव या समस्या निवारण निर्देश सीधे प्रदान नहीं कर सकता। कृपया रुकें और वर्तमान स्वीकृत साइट SOP और एक योग्य पर्यवेक्षक से परामर्श करें।",
    "Spanish": "No puedo proporcionar instrucciones directas de operación, reparación, aislamiento o solución de problemas del equipo. Deténgase y consulte el SOP aprobado del sitio y a un supervisor calificado.",
    "German": "Ich kann keine Anweisungen zur Bedienung, Reparatur, Freischaltung oder Fehlerbehebung von Geräten erteilen. Bitte stoppen Sie und konsultieren Sie die freigegebene SOP des Werks sowie einen qualifizierten Vorgesetzten.",
    "French": "Je ne peux pas fournir directement d'instructions de fonctionnement, de réparation, d'isolation ou de dépannage d'équipement. Arrêtez-vous et consultez la SOP approuvée du site et un superviseur qualifié.",
    "Japanese": "装置の操作、修理、遮断、またはトラブルシューティング手順を直接指示することはできません。作業を中止し、承認された現場のSOPおよび有資格の管理者に相談してください。",
}

VOICE_MISSING_DETAILS_PROMPTS: dict[str, str] = {
    "English": "Please specify the {missing} so I can search the manual and generate the SOP.",
    "Hindi": "कृपया {missing} बताएं ताकि मैं मैन्युअल में खोज कर सकूं और SOP तैयार कर सकूं।",
    "Spanish": "Por favor especifique {missing} para que pueda buscar en el manual y generar el SOP.",
    "German": "Bitte geben Sie {missing} an, damit ich das Handbuch durchsuchen und die SOP erstellen kann.",
    "French": "Veuillez préciser {missing} afin que je puisse rechercher dans le manuel et générer la SOP.",
    "Japanese": "マニュアルを検索してSOPを生成するために、{missing}を指定してください。",
}

LOCALIZED_FIELD_NAMES: dict[str, dict[str, str]] = {
    "Hindi": {
        "machine/equipment name": "मशीन/उपकरण का नाम",
        "specific maintenance procedure or symptom": "विशिष्ट रखरखाव प्रक्रिया या लक्षण",
    },
    "Spanish": {
        "machine/equipment name": "el nombre de la máquina/equipo",
        "specific maintenance procedure or symptom": "el procedimiento específico o síntoma",
    },
    "German": {
        "machine/equipment name": "den Maschinen-/Gerätenamen",
        "specific maintenance procedure or symptom": "das spezifische Wartungsverfahren oder Symptom",
    },
    "French": {
        "machine/equipment name": "le nom de la machine/équipement",
        "specific maintenance procedure or symptom": "la procédure spécifique ou le symptôme",
    },
    "Japanese": {
        "machine/equipment name": "機械/装置名",
        "specific maintenance procedure or symptom": "具体的なメンテナンス手順または症状",
    },
}

INDIRECT_SYMPTOM_PATTERN = re.compile(
    r"\b("
    r"nois\w*|sound\w*|screech\w*|squeal\w*|grind\w*|hum\w*|knock\w*|rattl\w*|whistl\w*|"
    r"vibrat\w*|shak\w*|oscillat\w*|"
    r"overheat\w*|heat\w*|hot|warm|temperat\w*|cooling\s+(?:loss|fail\w*|fault\w*|issue\w*|problem\w*)|"
    r"smok\w*|burn\w*|burnt|odor\w*|smell\w*|"
    r"leak\w*|fluid\w*|oil\w*|spill\w*|"
    r"trip\w*|shutdown|shut\s*down|"
    r"stop\w*|stuck|jam\w*|slow|frozen|fail\w*|"
    r"abnormal\w*|anomal\w*|weird|strange|wrong|"
    r"troubl\w*|issue\w*|problem\w*|broken|fault\w*|error\w*|alarm\w*|warning\w*|"
    r"won'?t\s+(?:start|run|move|turn|stop)|not\s+(?:working|running|starting|moving|turning)"
    r")\b",
    re.IGNORECASE,
)

INDIRECT_QUESTION_PATTERN = re.compile(
    r"\b("
    r"why\s+(is|did|does|won'?t|has)|"
    r"what('s|\s+is)\s+(wrong|happening|the\s+problem|the\s+issue)|"
    r"how\s+do\s+i\s+(fix|troubleshoot|solve|handle|deal\s+with)|"
    r"what\s+should\s+i\s+do|"
    r"help(\s+me)?\s+(with|troubleshoot|fix)|"
    r"can\s+you\s+(fix|help|diagnose)"
    r")\b",
    re.IGNORECASE,
)


def is_indirect_query(
    text: str,
    machine_name: str | None = None,
    error_code: str | None = None,
    document_id: int | None = None,
) -> bool:
    """Detect whether a query is an indirect symptom or vague troubleshooting inquiry

    lacking required diagnostic evidence (specific machine model or exact fault code).
    """
    clean = text.strip()
    if not clean:
        return False
    if error_code and error_code.strip():
        return False
    if machine_name and len(machine_name.strip()) > 3:
        if machine_name.strip().lower() not in {"machine", "equipment", "device", "unit", "system", "tool"}:
            return False
    if document_id is not None:
        return False
    if GREETING_PATTERN.search(clean):
        return False
    if is_operational_request(clean):
        return False
    if re.search(r"^\s*(what|which)\b.*\b(require|say|state|specify|describe|explain|mean|protocol|policy|standard|spec|guide|manual)\b", clean, re.IGNORECASE):
        return False
    if KB_LOOKUP_QUESTION.search(clean) and KB_TOPIC_REFERENCE.search(clean):
        return False
    if re.search(r"\b(bypass\s+the\s+interlock|lock\s*out\s+valve|de-energize\s+this\s+line|isolate\s+the\s+line)\b", clean, re.IGNORECASE):
        return False
    return bool(INDIRECT_SYMPTOM_PATTERN.search(clean) or INDIRECT_QUESTION_PATTERN.search(clean))


class EscalationRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    reason: str = Field(min_length=3, max_length=500)


class VoiceKnowledgeRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    language: str | None = "English"


class VoiceGenerateSopRequest(BaseModel):
    machine_name: str = ""
    procedure_name: str = ""
    error_code: str = ""
    expert_notes: str = ""
    session_id: str = ""
    language: str | None = "English"


class SessionRequest(BaseModel):
    kind: str = Field(default="text", pattern="^(text|voice)$")


class ChatRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    document_id: int | None = None
    image: str | None = None
    audio: str | None = None
    session_id: str | None = None
    generate_sop: bool | None = False
    machine_name: str | None = None
    machine_type: str | None = None
    error_code: str | None = None
    expert_notes: str | None = None
    module_name: str | None = None
    language: str | None = "English"


class CreateChatSessionRequest(BaseModel):
    name: str | None = None
    machine_id: str | None = None
    error_code: str | None = None
    error_image: str | None = None
    problem: str | None = None
    module_name: str | None = None
    member_ids: list[int] | None = None
    initial_query: str | None = None


class AddChatMemberRequest(BaseModel):
    user_id: int | None = None
    username: str | None = None


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
        # Initialize core tables
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
                mode TEXT NOT NULL DEFAULT 'grounded',
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

        # Initialize auth tables
        auth.init_auth_tables(connection)

        # Initialize SOP tables
        sop.init_sop_tables(connection)
        detective.init_detective_tables(connection)

        # Migrations
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(queries)")}
        if "mode" not in columns:
            connection.execute(
                "ALTER TABLE queries ADD COLUMN mode TEXT NOT NULL DEFAULT 'grounded'"
            )
            connection.execute(
                "UPDATE queries SET mode = "
                "CASE WHEN grounded = 1 THEN 'grounded' ELSE 'abstained' END"
            )

        doc_columns = {row["name"] for row in connection.execute("PRAGMA table_info(documents)")}
        if "image_path" not in doc_columns:
            connection.execute(
                "ALTER TABLE documents ADD COLUMN image_path TEXT"
            )

        # Add new document columns
        if "uploaded_by" not in doc_columns:
            connection.execute("ALTER TABLE documents ADD COLUMN uploaded_by INTEGER")
        if "rejected" not in doc_columns:
            connection.execute("ALTER TABLE documents ADD COLUMN rejected INTEGER NOT NULL DEFAULT 0")
        if "page_count" not in doc_columns:
            connection.execute("ALTER TABLE documents ADD COLUMN page_count INTEGER")

        # Add new session columns
        session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
        if "room_id" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN room_id INTEGER")
        if "created_by" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN created_by INTEGER")
        if "machine_id" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN machine_id TEXT")
        if "machine_type" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN machine_type TEXT")
        if "error_code" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN error_code TEXT")
        if "error_image" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN error_image TEXT")
        if "problem_title" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN problem_title TEXT")
        if "name" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN name TEXT")
        if "module_name" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN module_name TEXT")
        if "status" not in session_columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN status TEXT DEFAULT 'active'")

        # Create session_members table
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_members (
                session_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'member',
                joined_at TEXT NOT NULL,
                PRIMARY KEY (session_id, user_id),
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )

        # Add new session_message columns
        msg_columns = {row["name"] for row in connection.execute("PRAGMA table_info(session_messages)")}
        if "user_id" not in msg_columns:
            connection.execute("ALTER TABLE session_messages ADD COLUMN user_id INTEGER")
        if "sop_id" not in msg_columns:
            connection.execute("ALTER TABLE session_messages ADD COLUMN sop_id INTEGER")

        # Create document_assets table
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS document_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                page_number INTEGER,
                kind TEXT NOT NULL CHECK(kind IN ('embedded', 'page', 'upload')),
                file_path TEXT NOT NULL,
                caption TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
            """
        )

        # Create app_settings table for dynamic system configurations (model selection, etc.)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        # Ensure default admin and room
        first_boot = auth.ensure_default_admin(connection)
        auth.ensure_default_room(connection)

        # Seed demo documents
        document_count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        if document_count == 0:
            for path in sorted(KNOWLEDGE_DIR.glob("*.md")):
                connection.execute(
                    "INSERT INTO documents (name, body, source, approved, created_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (path.name, path.read_text(encoding="utf-8"), "Demo knowledge base", now()),
                )

        # Create shared text session for the default room
        room = connection.execute("SELECT id FROM rooms WHERE name = 'Shared floor'").fetchone()
        if room:
            room_id = room["id"]
            existing_session = connection.execute(
                "SELECT id FROM sessions WHERE kind = 'text' AND room_id = ?",
                (room_id,),
            ).fetchone()
            if not existing_session:
                session_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO sessions (id, kind, room_id, created_at, updated_at) "
                    "VALUES (?, 'text', ?, ?, ?)",
                    (session_id, room_id, now(), now()),
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
    session_id: str,
    role: str,
    content: str,
    modality: str = "text",
    user_id: Optional[int] = None,
    sop_id: Optional[int] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> None:
    content = content.strip()
    if not content:
        return
    if connection is not None:
        connection.execute(
            "INSERT INTO session_messages "
            "(session_id, role, content, modality, user_id, sop_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, modality, user_id, sop_id, now()),
        )
        connection.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (now(), session_id),
        )
    else:
        with connect() as conn:
            conn.execute(
                "INSERT INTO session_messages "
                "(session_id, role, content, modality, user_id, sop_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, role, content, modality, user_id, sop_id, now()),
            )
            conn.execute(
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
PAGE_MARKER_REGEX = re.compile(r"\[\[page:(\d+)\]\]")


def split_passages(text: str) -> list[str]:
    """Split a document into retrievable passages.

    Keeps the [[page:N]] line on every chunk from that page.
    A Markdown heading is kept with the section it introduces. Splitting it off
    on its own produced title-only passages such as "# Document review and
    escalation", which then ranked as the best citation for broad queries even
    though they contain no usable evidence.
    """
    passages: list[str] = []
    pending_heading = ""
    current_page = ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        page_match = PAGE_MARKER_REGEX.search(block)
        if page_match:
            current_page = f"[[page:{page_match.group(1)}]]"
            if block == current_page:
                continue

        if HEADING_PATTERN.match(block) and "\n" not in block:
            pending_heading = block
            continue
        if pending_heading:
            block = f"{pending_heading}\n{block}"
            pending_heading = ""

        if current_page and not PAGE_MARKER_REGEX.search(block):
            block = f"{current_page}\n{block}"

        if len(block) <= 1200:
            passages.append(block)
            continue
        for index in range(0, len(block), 900):
            chunk = block[index:index + 1000]
            if current_page and not PAGE_MARKER_REGEX.search(chunk):
                chunk = f"{current_page}\n{chunk}"
            passages.append(chunk)

    if pending_heading:
        h_block = f"{current_page}\n{pending_heading}" if current_page else pending_heading
        passages.append(h_block)
    return passages


def retrieve(
    query: str,
    documents: list[sqlite3.Row],
    limit: int = 3,
    ai_intent: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Score every passage in the approved corpus against the query and AI-understood problem intent.

    When ai_intent is provided, the AI decides what technical concepts, procedures, failure
    modes, and component terminology to look for based on understanding the question and problem,
    rather than solely relying on literal words in the question.

    Args:
        query: The search query
        documents: List of document rows
        limit: Maximum number of passages to return (default 3, use 12 for SOP generation)
        ai_intent: Optional AI problem understanding containing search queries & technical concepts
    """
    all_passages = [
        (document, passage, set(tokenize(passage)))
        for document in documents
        for passage in split_passages(document["body"])
    ]
    if not all_passages:
        return []

    total_passages = len(all_passages)

    if not ai_intent:
        # Standard baseline lexical retrieval
        query_terms = set(tokenize(query))
        if not query_terms:
            return []

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
        return candidates[:limit]

    # --- AI Problem Understanding Multi-Target Retrieval ---
    search_queries = list(ai_intent.get("search_queries") or [query])
    if query not in search_queries:
        search_queries.append(query)

    ai_concepts = set(ai_intent.get("technical_concepts") or [])

    passage_candidates: dict[tuple[int, str], dict[str, Any]] = {}

    for sq in search_queries:
        sq_terms = set(tokenize(sq))
        if not sq_terms:
            continue

        doc_freq = {
            term: sum(term in terms for _, _, terms in all_passages)
            for term in sq_terms
        }
        ans_terms = {term for term in sq_terms if doc_freq[term] > 0}
        if not ans_terms:
            continue

        def sq_weight(term: str) -> float:
            return math.log(1 + total_passages / (1 + doc_freq[term]))

        max_w = sum(sq_weight(term) for term in ans_terms)
        term_count = len(sq_terms)

        for document, passage, terms in all_passages:
            overlap = ans_terms & terms
            if not overlap:
                continue

            weighted_matches = sum(sq_weight(term) for term in overlap)
            score = weighted_matches / max_w if max_w else 0.0
            coverage = len(overlap) / term_count if term_count else 0.0
            concept_overlap = len(ai_concepts & terms) if ai_concepts else 0

            key = (document["id"], passage)
            if key not in passage_candidates:
                passage_candidates[key] = {
                    "document_id": document["id"],
                    "document_name": document["name"],
                    "source": document["source"],
                    "passage": passage,
                    "score": round(score, 3),
                    "coverage": round(coverage, 3),
                    "matched_terms": len(overlap),
                    "concept_overlap": concept_overlap,
                }
            else:
                existing = passage_candidates[key]
                if score > existing["score"]:
                    existing["score"] = round(score, 3)
                if coverage > existing["coverage"]:
                    existing["coverage"] = round(coverage, 3)
                if len(overlap) > existing["matched_terms"]:
                    existing["matched_terms"] = len(overlap)
                existing["concept_overlap"] = max(existing["concept_overlap"], concept_overlap)

    candidates = list(passage_candidates.values())
    candidates.sort(
        key=lambda item: (item["score"], item.get("concept_overlap", 0), item["matched_terms"]),
        reverse=True,
    )
    return candidates[:limit]


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


def get_app_setting(key: str, default: str = "") -> str:
    """Retrieve an application setting from SQLite app_settings table."""
    try:
        with connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
            if row:
                return str(row["value"])
    except Exception:
        pass
    return default


def set_app_setting(key: str, value: str) -> None:
    """Persist an application setting in SQLite app_settings table."""
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, now()),
        )


def get_active_model_config() -> dict[str, Any]:
    """Return the active AI model engine configuration and available models."""
    db_provider = get_app_setting("llm_provider", "")
    if is_mock_test_active():
        active_provider = "nvidia"
    elif db_provider in ("gemini", "nvidia"):
        active_provider = db_provider
    elif LLM_PROVIDER in ("gemini", "nvidia"):
        active_provider = LLM_PROVIDER
    else:
        active_provider = "nvidia"

    nvidia_model = get_app_setting("nvidia_model", LLM_MODEL)
    gemini_model = get_app_setting("gemini_model", GEMINI_TEXT_MODEL)

    active_model = gemini_model if active_provider == "gemini" else nvidia_model
    has_active_key = bool(GEMINI_API_KEY) if active_provider == "gemini" else bool(LLM_API_KEY)

    return {
        "provider": active_provider,
        "active_model": active_model,
        "nvidia_model": nvidia_model,
        "gemini_model": gemini_model,
        "has_active_key": has_active_key,
        "has_gemini_key": bool(GEMINI_API_KEY),
        "has_nvidia_key": bool(LLM_API_KEY),
        "available_gemini_models": [
            {"id": "gemini-3.5-flash-lite", "name": "Gemini 3.5 Flash Lite (Fastest & Ultra-Reliable)"},
            {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash (Deep Engineering Reasoning)"},
        ],
        "available_nvidia_models": [
            {"id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "name": "Nemotron 3 Nano Omni (30B Reasoning)"},
            {"id": "meta/llama-3.3-70b-instruct", "name": "Meta Llama 3.3 70B Instruct"},
            {"id": "mistralai/mistral-large-2-instruct", "name": "Mistral Large 2 Instruct"},
        ],
    }


async def call_active_llm(
    system_prompt: str,
    user_prompt: str | list[dict[str, Any]],
    image: str | None = None,
    audio: str | None = None,
    json_mode: bool = False,
    override_provider: str | None = None,
    override_model: str | None = None,
) -> str:
    """Unified LLM dispatcher routing queries to Google Gemini API or NVIDIA NIM."""
    config = get_active_model_config()
    provider = override_provider or config["provider"]
    model = override_model or (config["gemini_model"] if provider == "gemini" else config["nvidia_model"])

    if provider == "gemini":
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        parts: list[dict[str, Any]] = []
        if image and isinstance(image, str) and image.startswith("data:"):
            try:
                header, b64data = image.split(",", 1)
                mime_type = header.split(";")[0].replace("data:", "")
                parts.append({"inline_data": {"mime_type": mime_type, "data": b64data}})
            except Exception:
                pass

        if isinstance(user_prompt, list):
            for p in user_prompt:
                if isinstance(p, dict):
                    if p.get("type") == "text":
                        parts.append({"text": p.get("text", "")})
                    elif p.get("type") == "image_url":
                        url = p.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            try:
                                header, b64data = url.split(",", 1)
                                mime_type = header.split(";")[0].replace("data:", "")
                                parts.append({"inline_data": {"mime_type": mime_type, "data": b64data}})
                            except Exception:
                                pass
        else:
            parts.append({"text": str(user_prompt)})

        generation_config: dict[str, Any] = {
            "temperature": 0.1 if json_mode else 0.2,
            "maxOutputTokens": 3072,
        }
        if json_mode:
            generation_config["response_mime_type"] = "application/json"

        req_body: dict[str, Any] = {
            "contents": [{"parts": parts}],
            "generationConfig": generation_config,
        }
        if system_prompt:
            req_body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        candidate_models = [model]
        if model != "gemini-3.5-flash-lite":
            candidate_models.append("gemini-3.5-flash-lite")

        last_err = None
        for candidate in candidate_models:
            url = f"{GEMINI_API_BASE_URL}/models/{candidate}:generateContent?key={GEMINI_API_KEY}"
            try:
                async with httpx.AsyncClient(timeout=45) as client:
                    resp = await client.post(url, json=req_body)
                    if resp.status_code == 503 and candidate != "gemini-3.5-flash-lite":
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    candidates = data.get("candidates") or []
                    if candidates:
                        c_parts = candidates[0].get("content", {}).get("parts") or []
                        if c_parts:
                            return c_parts[0].get("text", "")
                    return ""
            except Exception as exc:
                last_err = exc
        raise last_err or RuntimeError("Gemini API call returned empty response.")

    else:
        # NVIDIA NIM / OpenAI-compatible endpoint
        if not LLM_API_KEY:
            raise RuntimeError("LLM_API_KEY is not configured.")

        user_content = user_prompt
        if not isinstance(user_content, list) and (image or audio):
            user_content = _build_user_content(str(user_prompt), "", image, audio)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        payload: dict[str, Any] = {
            "model": model,
            "temperature": 0.1 if json_mode else 0.2,
            "max_tokens": 3072,
            "messages": messages,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{LLM_API_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            msg = choices[0].get("message", {}) if choices else {}
            ans = ""
            if isinstance(msg, dict):
                ans = msg.get("content") or msg.get("reasoning_content") or ""
                if not ans and isinstance(msg.get("content"), list):
                    ans = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in msg["content"])
            if not ans and choices:
                ans = choices[0].get("text", "")
            return ans or ""


def is_mock_test_active() -> bool:
    """Check if pytest mock LLM client is monkeypatched or pytest is executing without live flag."""
    if "PYTEST_CURRENT_TEST" in os.environ and not os.environ.get("RUN_LIVE_LLM_TESTS"):
        return True
    try:
        client_cls = getattr(httpx, "AsyncClient", None)
        return bool(client_cls and getattr(client_cls, "__name__", "") == "FakeClient")
    except Exception:
        return False


def _heuristic_problem_understanding(
    query: str,
    machine_name: str | None = None,
    machine_type: str | None = None,
    error_code: str | None = None,
    module_name: str | None = None,
    expert_notes: str | None = None,
) -> dict[str, Any]:
    """Heuristic problem understanding mapping conversational symptoms and context to engineering concepts."""
    q_lower = query.lower()

    detected_errors = re.findall(r"\b[A-Za-z]{1,4}[-_]?\d{2,5}\b", query)
    if error_code and error_code not in detected_errors:
        detected_errors.append(error_code)

    search_queries = [query]
    technical_concepts = list(tokenize(query))

    equipment = machine_name or ""
    if not equipment:
        for eq_term in ["lathe", "robotic arm", "packaging arm", "conveyor", "press", "pump", "valve", "spindle", "motor"]:
            if eq_term in q_lower:
                equipment = eq_term
                break

    inferred_problems = []

    # 1. Calibration & Alignment
    if any(k in q_lower for k in ["calibrat", "align", "runout", "truing", "dial indicator"]):
        inferred_problems.append("alignment and calibration verification")
        search_queries.append(f"{equipment} calibration alignment check dial indicator")
        technical_concepts.extend(["calibrat", "align", "runout", "spindle", "toler"])

    # 2. Overtorque / Stall / Axis Obstruction
    if any(k in q_lower for k in ["overtorque", "stall", "stopp", "stuck", "lockup", "resist", "wobbl"]):
        inferred_problems.append("motor overtorque stall and drive obstruction recovery")
        search_queries.append(f"{equipment} motor overtorque fault recovery procedure")
        search_queries.append("optical encoder alignment drive belt tension")
        technical_concepts.extend(["overtorqu", "stall", "motor", "encod", "belt", "tension", "coupl"])

    # 3. Vibration / Noise / Slideway Binding
    if any(k in q_lower for k in ["vibrat", "shak", "screech", "nois", "stick", "friction", "slideway"]):
        inferred_problems.append("mechanical vibration, bearing play, or slideway binding")
        search_queries.append(f"{equipment} carriage slideways lubrication bearing check")
        technical_concepts.extend(["vibrat", "bear", "slideway", "lubric", "gib"])

    # 4. Tension / Belt / Chain
    if any(k in q_lower for k in ["tension", "belt", "chain", "slack", "slip"]):
        inferred_problems.append("drive belt tensioning and torque specification")
        search_queries.append(f"{equipment} belt tensioning procedure torque")
        technical_concepts.extend(["tension", "belt", "torqu", "bolt", "tighten"])

    # 5. Leakage / Pressure / Hydraulics
    if any(k in q_lower for k in ["leak", "pressur", "gasket", "seal", "fluid", "hydraul"]):
        inferred_problems.append("hydraulic pressure isolation and seal gasket replacement")
        search_queries.append(f"{equipment} pressure isolation seal gasket replacement")
        technical_concepts.extend(["pressur", "isol", "seal", "gasket", "valv"])

    # 6. Policy / Review / Governance
    if any(k in q_lower for k in ["evidence policy", "document review policy", "approval threshold", "governance policy"]):
        inferred_problems.append("document review and escalation governance policy")
        search_queries.append("evidence coverage review threshold escalation policy")
        technical_concepts.extend(["evid", "coverag", "review", "polici", "threshold"])

    # 7. Error code specific
    for code in detected_errors:
        search_queries.append(f"{code} fault recovery procedure")
        technical_concepts.append(code.lower())

    if not inferred_problems:
        problem_summary = f"Investigation of {equipment or 'equipment'} operational query: {query[:80]}"
    else:
        problem_summary = f"Diagnosing {equipment + ' ' if equipment else ''}{' and '.join(inferred_problems)}"

    seen_sq = set()
    unique_sq = []
    for s in search_queries:
        if s.strip() and s.lower() not in seen_sq:
            seen_sq.add(s.lower())
            unique_sq.append(s.strip())

    seen_tc = set()
    unique_tc = []
    for t in technical_concepts:
        t_clean = t.lower().strip()
        if t_clean and t_clean not in seen_tc and len(t_clean) > 2:
            seen_tc.add(t_clean)
            unique_tc.append(t_clean)

    return {
        "understood_problem": problem_summary,
        "search_queries": unique_sq,
        "technical_concepts": unique_tc,
        "method": "semantic_rule",
    }


async def ai_understand_problem_for_rag(
    query: str,
    machine_name: str | None = None,
    machine_type: str | None = None,
    error_code: str | None = None,
    module_name: str | None = None,
    expert_notes: str | None = None,
) -> dict[str, Any]:
    """Use AI to understand the underlying equipment problem and decide RAG search targets.

    Analyzes the user's question, symptoms, and operational context to decide what technical
    manual topics, procedures, and terminology to search for, rather than just matching literal words.
    """
    rule_intent = _heuristic_problem_understanding(
        query, machine_name=machine_name, machine_type=machine_type,
        error_code=error_code, module_name=module_name, expert_notes=expert_notes
    )

    if is_mock_test_active():
        return rule_intent

    active_config = get_active_model_config()
    provider = active_config.get("provider", "gemini")
    has_key = bool(GEMINI_API_KEY) if provider == "gemini" else bool(LLM_API_KEY)
    if not has_key:
        return rule_intent

    system_prompt = (
        "You are an industrial diagnostics AI and RAG search query optimizer.\n"
        "Your task: understand the operator's actual equipment problem, symptoms, or maintenance intent—"
        "NOT just the superficial words they used—and decide what specific technical topics, procedures, "
        "failure modes, component names, and diagnostic keywords to search for in industrial manuals and SOPs.\n\n"
        "Respond strictly with a JSON object in this format:\n"
        "{\n"
        '  "understood_problem": "1-2 sentence engineering description of what is malfunctioning or being inquired about",\n'
        '  "search_queries": [\n'
        '    "primary technical search query targeting the core procedure or resolution in the manual",\n'
        '    "component or subsystem specific search query",\n'
        '    "diagnostic, error code, or calibration search query"\n'
        '  ],\n'
        '  "technical_concepts": ["term1", "term2", "term3", "term4", "term5"]\n'
        "}"
    )

    context_lines = []
    if machine_name:
        context_lines.append(f"Machine / Equipment: {machine_name} ({machine_type or ''})")
    if error_code:
        context_lines.append(f"Reported Error/Fault Code: {error_code}")
    if module_name:
        context_lines.append(f"Target Module/Manual: {module_name}")
    if expert_notes:
        context_lines.append(f"Operator Field Notes: {expert_notes}")

    context_block = "\n".join(context_lines) if context_lines else "None provided"
    user_prompt = (
        f"User Input: {query}\n\n"
        f"Operational Context:\n{context_block}\n\n"
        "Analyze the underlying problem and formulate targeted RAG search queries and technical concepts."
    )

    try:
        llm_response = await call_active_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            json_mode=True,
        )
        clean = (llm_response or "").strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)
        parsed = json.loads(clean)
        if isinstance(parsed, dict) and parsed.get("search_queries"):
            sq = [str(q).strip() for q in parsed.get("search_queries", []) if str(q).strip()]
            tc = [str(c).strip() for c in parsed.get("technical_concepts", []) if str(c).strip()]
            if query not in sq:
                sq.append(query)
            return {
                "understood_problem": parsed.get("understood_problem") or rule_intent["understood_problem"],
                "search_queries": sq or rule_intent["search_queries"],
                "technical_concepts": tc or rule_intent["technical_concepts"],
                "method": "ai_generative",
            }
    except Exception as exc:
        logger.warning(f"AI problem understanding call failed ({exc}); using semantic rule intent")

    return rule_intent


async def generate_answer(
    query: str,
    citations: list[dict[str, Any]],
    image: str | None = None,
    audio: str | None = None,
    conversational: bool = False,
    language: str = "English",
) -> tuple[str, str]:
    lang_name = normalize_language(language)
    config = get_active_model_config()
    if not config["has_active_key"]:
        if conversational:
            reply = CONVERSATIONAL_REPLIES.get(lang_name, CONVERSATIONAL_REPLIES["English"])
            return reply, "conversational-extractive"
        if lang_name != "English":
            notices = {
                "Hindi": "पुनर्प्राप्त स्रोत उद्धरण नीचे दिखाए गए हैं। यह प्रणाली कार्य निर्देशों को स्वतः उत्पन्न या मान्य नहीं करती है।",
                "Spanish": "Los extractos de las fuentes recuperadas se muestran a continuación. Este sistema no genera ni valida instrucciones de trabajo.",
                "German": "Die abgerufenen Quellenauszüge sind unten aufgeführt. Dieses System erstellt oder validiert keine Arbeitsanweisungen.",
                "French": "Les extraits des sources récupérées sont présentés ci-dessous. Ce système ne génère ni ne valide d'instructions de travail.",
                "Japanese": "取得された引用抜粋は以下に表示されています。このシステムは作業指示書を直接生成または検証しません。",
            }
            return notices.get(lang_name, "Retrieved source excerpts are shown below. This system does not generate or validate work instructions."), "extractive"
        return "Retrieved source excerpts are shown below. This system does not generate or validate work instructions.", "extractive"
    fallback = (
        f"The model service was unavailable or returned no answer. Source excerpts are shown below; please retry."
        if lang_name == "English"
        else f"सेवा अनुपलब्ध थी। कृपया पुनः प्रयास करें।"
    )

    source_context = "\n\n".join(
        f"[{citation['reference_id']}] {citation['document_name']} ({citation['source']}):\n{citation['passage']}"
        for citation in citations
    ) or "No retrieved source excerpts."
    valid_reference_ids = {citation["reference_id"] for citation in citations}

    if conversational:
        system_content = (
            "You are an expert industrial engineering intelligence assistant. "
            "Answer greetings and meta questions briefly, warmly, and professionally. "
            "For domain questions, answer thoroughly using only the supplied source excerpts and cite every "
            "factual statement with the exact reference token, such as [S1]. "
            "When explaining workflows, architectures, or sequences, generate clean Mermaid.js diagrams "
            "using fenced code blocks (```mermaid ... ```). "
            "Never provide unauthorized plant equipment operation, repair, maintenance, isolation, adjustment, "
            "troubleshooting, bypass, or work-step instructions. "
            "If no sources are available, say so and suggest relevant knowledge-base topics. "
            "The source may be synthetic and must never be described as an approved plant SOP."
        )
    else:
        system_content = (
            "You are an expert industrial engineering intelligence assistant. "
            "Answer informational questions thoroughly and professionally using all supplied source excerpts. "
            "Structure your response as an intelligent, executive-grade engineering report with clear sections, "
            "bullet points, and technical precision. "
            "When illustrating workflows, architectures, electrical/mechanical stages, state transitions, or safety procedures, "
            "generate clean Mermaid.js diagrams using fenced code blocks (```mermaid ... ```) using flowchart TD/LR, sequenceDiagram, or stateDiagram-v2. "
            "This is an industrial safety domain: never provide unauthorized plant equipment operation, "
            "repair, maintenance, isolation, adjustment, troubleshooting, bypass, "
            "or work-step instructions. Do not convert source text into an unverified plant procedure. "
            "Do not infer missing facts. If sources do not support an answer, say so clearly. "
            "Cite every factual statement with the exact reference token, such as [S1], and cite every relevant excerpt. "
            "You may also receive an image or audio clip; describe or transcribe what you observe and relate it to the retrieved sources when relevant. "
            "The source may be synthetic and must never be described as an approved plant SOP."
        )

    if lang_name != "English":
        system_content += (
            f"\n\nCRITICAL LANGUAGE MANDATE: You MUST formulate your ENTIRE response strictly and exclusively in {lang_name}. "
            f"All explanations, section titles, steps, cautions, Mermaid diagram labels, and summaries MUST be written in {lang_name}. "
            f"Do NOT answer in English unless the user explicitly requested English."
        )

    user_content = _build_user_content(query, source_context, image, audio)

    last_exc = None
    for attempt in range(3):
        try:
            answer = await call_active_llm(
                system_prompt=system_content,
                user_prompt=user_content,
                image=image,
                audio=audio,
                json_mode=False,
            )
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
    title="MechTech AI Evidence API",
    version="1.0.0",
    description="Local-first, evidence-only SOP retrieval demo with review-gated ingestion.",
    lifespan=lifespan,
)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/invite/{code}", methods=["GET", "HEAD"], include_in_schema=False)
def frontend(code: str = "") -> FileResponse:
    return FileResponse(ROOT / "industrial_genai_sop_assistant.html")


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
def favicon() -> Response:
    # A tiny transparent ICO so the browser stops requesting a missing file.
    icon = (
        b"\x00\x00\x01\x00\x01\x00\x10\x10\x00\x00\x01\x00\x20\x00\x68\x04"
        b"\x00\x00\x16\x00\x00\x00"
    )
    payload = icon + b"\x00" * (4096 - len(icon))
    return Response(payload, media_type="image/x-icon")



@app.api_route("/live", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/live-audio", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/live_audio_chat.html", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/voice-chat", methods=["GET", "HEAD"], include_in_schema=False)
def live_audio_chat() -> FileResponse:
    return FileResponse(ROOT / "live_audio_chat.html")

@app.api_route("/api/health", methods=["GET", "HEAD"])
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "evidence-only", "model": "extractive retrieval"}


@app.get("/api/voice/config")
def voice_config() -> dict[str, str]:
    return {
        "model": GEMINI_LIVE_MODEL,
        "ws_url": "/api/voice/ws",
    }


# ---- Auth dependencies ----

def get_current_user(request: Request) -> Optional[auth.User]:
    """Dependency to get the current authenticated user from cookie."""
    session_token = request.cookies.get(auth.SESSION_COOKIE_NAME)
    if not session_token:
        return None

    with connect() as connection:
        user_row = auth.get_session_user(connection, session_token)
        if not user_row:
            return None

        return auth.User(
            id=user_row["id"],
            username=user_row["username"],
            display_name=user_row["display_name"],
            role=user_row["role"],
            must_change_password=bool(user_row["must_change_password"]),
        )


def require_auth(request: Request) -> auth.User:
    """Dependency that requires authentication."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def require_admin(user: auth.User = Depends(require_auth)) -> auth.User:
    """Dependency that requires admin role."""
    if user.role != "admin":
        raise HTTPException(403, "Admin access required")
    return user


@app.get("/api/uploads/{image_name}", include_in_schema=False)
def serve_uploaded_image(image_name: str, user: auth.User = Depends(require_auth)) -> FileResponse:
    """Serve an uploaded image file for inline display."""
    image_path = IMAGE_UPLOAD_DIR / image_name
    if not image_path.exists():
        raise HTTPException(404, "Image not found.")
    return FileResponse(image_path)




# ---- Auth routes ----

def is_cookie_secure(http_req: Request | None) -> bool:
    if auth.COOKIE_SECURE:
        return True
    if not http_req:
        return False
    scheme = http_req.url.scheme
    forwarded = http_req.headers.get("x-forwarded-proto", "")
    return scheme == "https" or forwarded.lower() == "https"


@app.post("/api/auth/login")
def login(request: auth.LoginRequest, response: Response, http_request: Request) -> dict[str, Any]:
    """Authenticate a user and set a session cookie."""
    with connect() as connection:
        user_row = auth.get_user_by_username(connection, request.username)
        if not user_row:
            raise HTTPException(401, "Invalid username or password")

        if user_row["disabled"]:
            raise HTTPException(403, "Account is disabled")

        if not auth.verify_password(
            request.password, user_row["password_salt"], user_row["password_hash"]
        ):
            raise HTTPException(401, "Invalid username or password")

        user_id = user_row["id"]
        session_token, expires_at = auth.create_session(connection, user_id)

    # Set cookie
    response.set_cookie(
        key=auth.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=is_cookie_secure(http_request),
        max_age=auth.SESSION_EXPIRY_DAYS * 24 * 60 * 60,
    )

    return {
        "id": user_id,
        "username": user_row["username"],
        "display_name": user_row["display_name"],
        "role": user_row["role"],
        "must_change_password": bool(user_row["must_change_password"]),
    }


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    """Clear the session cookie."""
    session_token = request.cookies.get(auth.SESSION_COOKIE_NAME)
    if session_token:
        with connect() as connection:
            auth.delete_session(connection, session_token)

    response.delete_cookie(key=auth.SESSION_COOKIE_NAME)
    return {"message": "Logged out"}


@app.get("/api/auth/me")
def get_me(user: auth.User = Depends(require_auth)) -> auth.User:
    """Get current user info."""
    return user


@app.post("/api/auth/password")
def change_password(
    request: auth.PasswordChangeRequest,
    user: auth.User = Depends(require_auth),
) -> dict[str, str]:
    """Change the current user's password."""
    with connect() as connection:
        auth.change_password(
            connection, user.id, request.current_password, request.new_password
        )
    return {"message": "Password changed"}


@app.get("/api/users")
def list_users(user: auth.User = Depends(require_admin)) -> dict[str, Any]:
    """List all users (admin only)."""
    with connect() as connection:
        users = connection.execute(
            "SELECT id, username, display_name, role, disabled, must_change_password, created_at "
            "FROM users ORDER BY username"
        ).fetchall()
    return {"users": [dict(row) for row in users]}


@app.post("/api/users", status_code=201)
def create_user(
    request: auth.CreateUserRequest,
    admin: auth.User = Depends(require_admin),
) -> dict[str, Any]:
    """Create a new user (admin only)."""
    with connect() as connection:
        # Check if username exists
        existing = auth.get_user_by_username(connection, request.username)
        if existing:
            raise HTTPException(409, "Username already exists")

        salt, password_hash = auth.hash_password(request.password)
        connection.execute(
            "INSERT INTO users (username, display_name, password_hash, password_salt, role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (request.username, request.display_name, password_hash, salt, request.role, now()),
        )
        user_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Add to default room
        room = connection.execute("SELECT id FROM rooms WHERE name = 'Shared floor'").fetchone()
        if room:
            connection.execute(
                "INSERT INTO room_members (room_id, user_id, joined_at) VALUES (?, ?, ?)",
                (room["id"], user_id, now()),
            )

    return {"id": user_id, "username": request.username, "role": request.role}


@app.post("/api/users/{user_id}/disable")
def disable_user(
    user_id: int,
    admin: auth.User = Depends(require_admin),
) -> dict[str, str]:
    """Disable a user (admin only)."""
    with connect() as connection:
        # Prevent disabling last admin
        admin_count = connection.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0"
        ).fetchone()[0]
        if admin_count <= 1:
            target = connection.execute(
                "SELECT role FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if target and target["role"] == "admin":
                raise HTTPException(403, "Cannot disable the last admin")

        # Prevent disabling self
        if user_id == admin.id:
            raise HTTPException(403, "Cannot disable yourself")

        result = connection.execute(
            "UPDATE users SET disabled = 1 WHERE id = ?", (user_id,)
        )
        if result.rowcount == 0:
            raise HTTPException(404, "User not found")

    return {"message": "User disabled"}


@app.post("/api/users/{user_id}/enable")
def enable_user(
    user_id: int,
    admin: auth.User = Depends(require_admin),
) -> dict[str, str]:
    """Enable a disabled user (admin only)."""
    with connect() as connection:
        result = connection.execute(
            "UPDATE users SET disabled = 0 WHERE id = ?", (user_id,)
        )
        if result.rowcount == 0:
            raise HTTPException(404, "User not found")

    return {"message": "User enabled"}


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


async def execute_voice_generate_sop(
    machine_name: str,
    procedure_name: str,
    error_code: str = "",
    expert_notes: str = "",
    session_id: str = "",
    language: str = "English",
) -> dict[str, Any]:
    """Analyze approved industrial manuals with the text model and generate a step-by-step SOP.

    If machine_name or procedure_name are missing or placeholder strings, returns status 'missing_details'
    so the conversational voice agent can ask the operator for the missing parameters.
    """
    cleaned_machine = machine_name.strip()
    cleaned_procedure = procedure_name.strip()
    lang = normalize_language(language)

    generic_placeholders = {"unknown", "n/a", "none", "machine", "equipment", "procedure", "sop", "task", "something", ""}
    missing: list[str] = []
    if not cleaned_machine or cleaned_machine.lower() in generic_placeholders:
        missing.append("machine/equipment name")
    if not cleaned_procedure or cleaned_procedure.lower() in generic_placeholders:
        missing.append("specific maintenance procedure or symptom")

    if missing:
        field_map = LOCALIZED_FIELD_NAMES.get(lang, {})
        localized_missing = [field_map.get(m, m) for m in missing]
        joined_sep = " तथा " if lang == "Hindi" else (" y " if lang == "Spanish" else (" und " if lang == "German" else " and "))
        joined_missing = joined_sep.join(localized_missing)
        prompt_template = VOICE_MISSING_DETAILS_PROMPTS.get(lang, VOICE_MISSING_DETAILS_PROMPTS["English"])
        prompt_question = prompt_template.format(missing=joined_missing)
        return {
            "status": "missing_details",
            "missing_fields": missing,
            "message": f"Required details missing: {', '.join(missing)}. {prompt_question}",
        }

    # Formulate engineering query
    query = f"{cleaned_machine} {cleaned_procedure}"
    if error_code.strip():
        query += f" error {error_code.strip()}"

    with connect() as conn:
        rows = conn.execute("SELECT * FROM documents WHERE approved = 1").fetchall()

    if not rows:
        return {
            "status": "no_manual_found",
            "message": "There are no approved manuals in the knowledge vault to generate this SOP.",
        }

    # Use AI problem understanding to plan RAG retrieval targets
    ai_intent = await ai_understand_problem_for_rag(
        query,
        machine_name=cleaned_machine,
        machine_type=cleaned_machine,
        error_code=error_code,
        expert_notes=expert_notes,
    )
    all_sop_matches = retrieve(query, rows, limit=12, ai_intent=ai_intent)
    if not all_sop_matches:
        all_sop_matches = retrieve(query, rows, limit=12)

    if not all_sop_matches:
        return {
            "status": "no_manual_found",
            "message": f"No relevant manual passages found matching {cleaned_machine} and {cleaned_procedure}.",
        }

    # Retrieve figure assets
    passage_page_numbers = set()
    used_doc_ids = set()
    for match in all_sop_matches:
        used_doc_ids.add(match["document_id"])
        for line in match["passage"].splitlines():
            if "[[page:" in line:
                try:
                    p = int(line.split("[[page:")[1].split("]]")[0])
                    passage_page_numbers.add(p)
                except (ValueError, IndexError):
                    pass

    query_clauses = []
    params = []
    if passage_page_numbers:
        placeholders = ",".join("?" * len(passage_page_numbers))
        query_clauses.append(f"page_number IN ({placeholders})")
        params.extend(list(passage_page_numbers))
    if used_doc_ids:
        doc_placeholders = ",".join("?" * len(used_doc_ids))
        query_clauses.append(f"(document_id IN ({doc_placeholders}) AND kind = 'upload')")
        params.extend(list(used_doc_ids))

    with connect() as conn:
        if query_clauses:
            where_clause = " OR ".join(query_clauses)
            asset_rows = conn.execute(
                f"SELECT id, document_id, page_number, kind, file_path, caption FROM document_assets WHERE {where_clause}",
                params,
            ).fetchall()
            assets = [dict(a) for a in asset_rows]
        else:
            assets = []

    # Build SOP using the text model
    with connect() as ref_conn:
        prior_references = sop.find_similar_sops(
            ref_conn, query, error_code, cleaned_machine
        )
        expert_learnings = sop.find_relevant_expert_knowledge(
            ref_conn, query, error_code, cleaned_machine
        )
    f_map = {f"F{i}": a["id"] for i, a in enumerate(assets, start=1)}
    f_map.update({a["id"]: a["id"] for a in assets})
    sop_data = await sop.build_sop_from_passages(
        query,
        all_sop_matches,
        assets,
        machine_name=cleaned_machine,
        machine_type=cleaned_machine,
        error_code=error_code,
        expert_notes=expert_notes,
        prior_references=prior_references,
        expert_learnings=expert_learnings,
        language=lang,
    )

    citations = _build_citations(all_sop_matches)
    valid_citation_ids = {c["reference_id"] for c in citations}
    valid_figure_ids = {a["id"] for a in assets}
    sop_data = sop.finalize_sop_answer(sop_data, valid_citation_ids, valid_figure_ids, f_map=f_map)

    # Fallback to smart extractive extraction if needed
    if sop_data.get("abstain") and all_sop_matches:
        sop_data = sop.extract_steps_from_passages(query, all_sop_matches, assets, language=lang)
        sop_data = sop.finalize_sop_answer(sop_data, valid_citation_ids, valid_figure_ids, f_map=f_map)

    sop_data = sop.attach_part_visuals(
        sop_data,
        assets,
        passages=all_sop_matches,
        query=query,
        error_code=error_code,
    )

    sop_data["machine_name"] = cleaned_machine
    sop_data["procedure_name"] = cleaned_procedure
    if error_code:
        sop_data["error_code"] = error_code
        sop_data["machine"] = sop_data.get("machine") or machine_name or "Fagor CNC 8055"
        sop_data["source"] = sop_data.get("source") or (
            all_sop_matches[0].get("document_name", "Approved machine manual")
            if all_sop_matches else "Approved machine manual"
        )
    if expert_notes:
        sop_data["expert_notes"] = expert_notes
    sop_data["citations"] = citations
    sop_data["figures"] = assets
    sop_data["rag_strategy"] = ai_intent
    sop_data["previous_sop_references"] = prior_references
    sop_data["expert_learnings"] = [
        {
            "id": entry["id"],
            "detection": entry["detection"],
            "solution": entry["solution"],
            "outcome": entry["outcome"],
            "similarity": entry["similarity"],
        }
        for entry in expert_learnings
    ]

    # Save to database
    valid_session_id = ensure_session(session_id, "voice")
    with connect() as conn:
        admin_row = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        author_id = admin_row["id"] if admin_row else 1

        room_row = conn.execute("SELECT id FROM rooms ORDER BY id ASC LIMIT 1").fetchone()
        room_id = room_row["id"] if room_row else 1

        conn.execute(
            "INSERT INTO session_messages (session_id, role, content, modality, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (valid_session_id, "assistant", f"Generated SOP for {query}", "voice", author_id, now()),
        )
        msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        sop_id = sop.create_sop(
            conn,
            room_id,
            valid_session_id,
            msg_id,
            author_id,
            query,
            sop_data,
        )

        conn.execute("UPDATE session_messages SET sop_id = ? WHERE id = ?", (sop_id, msg_id))
    sop_data["id"] = sop_id

    steps_count = len(sop_data.get("steps", []))
    cautions = [s["caution"] for s in sop_data.get("steps", []) if s.get("caution")]
    caution_summary = f" Crucial safety warning: {cautions[0]}." if cautions else ""

    if lang == "Hindi":
        spoken_summary = (
            f"मैंने मैन्युअल का विश्लेषण करके {sop_data.get('title', cleaned_procedure)} के लिए {steps_count}-चरणीय SOP तैयार किया है।{caution_summary} "
            f"पूरी SOP प्रक्रिया और PDF अब आपकी स्क्रीन पर तैयार है।"
        )
    elif lang == "Spanish":
        spoken_summary = (
            f"He analizado el manual y he generado un SOP de {steps_count} pasos para {sop_data.get('title', cleaned_procedure)}.{caution_summary} "
            f"El procedimiento completo y la descarga en PDF ya están listos en su pantalla."
        )
    elif lang == "German":
        spoken_summary = (
            f"Ich habe das Handbuch analysiert und eine {steps_count}-stufige SOP für {sop_data.get('title', cleaned_procedure)} erstellt.{caution_summary} "
            f"Die vollständige SOP und der PDF-Export stehen jetzt auf Ihrem Bildschirm bereit."
        )
    elif lang == "French":
        spoken_summary = (
            f"J'ai analysé le manuel et généré une SOP en {steps_count} étapes pour {sop_data.get('title', cleaned_procedure)}.{caution_summary} "
            f"La fiche complète et l'export PDF sont prêts sur votre écran."
        )
    elif lang == "Japanese":
        spoken_summary = (
            f"マニュアルを分析し、{sop_data.get('title', cleaned_procedure)}の{steps_count}ステップのSOPを作成しました。{caution_summary} "
            f"完全な手順カードとPDF出力が画面に表示されています。"
        )
    else:
        spoken_summary = (
            f"I have analyzed the manual using the engineering text model and generated a {steps_count}-step SOP "
            f"for {sop_data.get('title', cleaned_procedure)}.{caution_summary} "
            f"The complete step-by-step SOP card and PDF export are now ready on your screen."
        )

    return {
        "status": "success",
        "sop_id": sop_id,
        "title": sop_data.get("title", f"SOP: {cleaned_procedure}"),
        "summary": sop_data.get("summary", ""),
        "total_steps": steps_count,
        "steps": sop_data.get("steps", []),
        "cautions": cautions,
        "checks": sop_data.get("checks", []),
        "pdf_export_url": f"/api/sops/{sop_id}/export",
        "sop_data": sop_data,
        "message": spoken_summary,
    }


async def handle_live_tool_call(upstream: Any, tool_call: dict[str, Any], session_id: str = "") -> list[dict[str, Any]]:
    """Execute supported Live tools on the server and answer Gemini directly."""
    function_responses = []
    display_results = []
    for call in tool_call.get("functionCalls", []):
        name = call.get("name", "")
        call_id = call.get("id", "")
        args = call.get("args", {})

        if name == "search_knowledge_base":
            query = str(args.get("query", "")).strip()
            if not query:
                result = {"error": "A non-empty query is required."}
            else:
                result = await asyncio.to_thread(build_voice_knowledge, query)
        elif name == "generate_sop":
            machine_name = str(args.get("machine_name", "")).strip()
            procedure_name = str(args.get("procedure_name", "")).strip()
            error_code = str(args.get("error_code", "")).strip()
            expert_notes = str(args.get("expert_notes", "")).strip()
            tool_lang = str(args.get("language", "")).strip() or "English"
            result = await execute_voice_generate_sop(
                machine_name=machine_name,
                procedure_name=procedure_name,
                error_code=error_code,
                expert_notes=expert_notes,
                session_id=session_id,
                language=tool_lang,
            )
        else:
            result = {"error": f"Unknown tool: {name}"}

        display_results.append(result)
        function_responses.append({
            "name": name,
            "id": call_id,
            "response": {"result": json.dumps(result, ensure_ascii=True)},
        })

    if upstream and hasattr(upstream, "send"):
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
    session_token = websocket.cookies.get(auth.SESSION_COOKIE_NAME)
    if not session_token:
        await websocket.close(code=4401, reason="Authentication required")
        return
    with connect() as conn:
        user_row = auth.get_session_user(conn, session_token)
        if not user_row:
            await websocket.close(code=4401, reason="Authentication required")
            return

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
                        results = await handle_live_tool_call(upstream, payload["toolCall"], session_id=session_id)
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
def list_documents(user: auth.User = Depends(require_auth)) -> dict[str, Any]:
    """List all documents with asset count."""
    with connect() as connection:
        rows = connection.execute(
            """SELECT d.id, d.name, d.source, d.image_path, d.approved, d.rejected,
                      d.uploaded_by, d.page_count, d.created_at, length(d.body) AS characters,
                      (SELECT COUNT(*) FROM document_assets WHERE document_id = d.id) AS asset_count
               FROM documents d
               ORDER BY d.id DESC"""
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


@app.post("/api/voice/generate-sop")
async def api_voice_generate_sop(request: VoiceGenerateSopRequest) -> dict[str, Any]:
    """Execute SOP generation from voice live model flow using the text model."""
    return await execute_voice_generate_sop(
        machine_name=request.machine_name,
        procedure_name=request.procedure_name,
        error_code=request.error_code,
        expert_notes=request.expert_notes,
        session_id=request.session_id,
        language=request.language or "English",
    )


@app.post("/api/documents", status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    """Upload a document with ingestion support for PDF, DOCX, and images."""
    IMAGE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(file.filename or "").name
    extension = Path(name).suffix.lower()
    if extension not in DOCUMENT_EXTENSIONS:
        raise HTTPException(415, "Upload a PDF, DOCX, TXT, Markdown, or image file (PNG, JPG, WEBP, BMP, TIFF).")
    if MAX_UPLOAD_BYTES is not None:
        payload = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(payload) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"The maximum upload size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    else:
        payload = await file.read()

    temp_path = None
    saved_img_path = None
    try:
        with connect() as connection:
            if extension in IMAGE_EXTENSIONS:
                saved_img_path, body = prepare_image_for_storage(payload, name)
                if len(body.strip()) < 20:
                    saved_img_path.unlink(missing_ok=True)
                    raise HTTPException(422, "No searchable text was found in this file.")
                cursor = connection.execute(
                    """INSERT INTO documents (name, body, source, image_path, approved, uploaded_by, page_count, created_at)
                       VALUES (?, ?, 'Uploaded; review required', ?, 0, ?, NULL, ?)""",
                    (name, body, saved_img_path.name, user.id, now()),
                )
                document_id = cursor.lastrowid
                page_count = None

                # Create upload asset in assets/{document_id}/
                asset_dir = IMAGE_UPLOAD_DIR / "assets" / str(document_id)
                asset_dir.mkdir(parents=True, exist_ok=True)
                asset_path = asset_dir / saved_img_path.name
                import shutil
                shutil.copy(saved_img_path, asset_path)
                connection.execute(
                    """INSERT INTO document_assets
                       (document_id, page_number, kind, file_path, caption, created_at)
                       VALUES (?, NULL, 'upload', ?, ?, ?)""",
                    (document_id, str(asset_path), Path(name).stem, now()),
                )
                asset_ids = [connection.execute("SELECT last_insert_rowid()").fetchone()[0]]

            elif extension == ".pdf":
                temp_path = IMAGE_UPLOAD_DIR / f"temp_{uuid.uuid4()}{extension}"
                temp_path.write_bytes(payload)
                cursor = connection.execute(
                    """INSERT INTO documents (name, body, source, image_path, approved, uploaded_by, page_count, created_at)
                       VALUES (?, '', 'Uploaded; review required', NULL, 0, ?, NULL, ?)""",
                    (name, user.id, now()),
                )
                document_id = cursor.lastrowid
                body, asset_ids = ingest.ingest_pdf(temp_path, document_id, IMAGE_UPLOAD_DIR, connection)
                page_count = body.count("[[page:")
                if len(body.strip()) < 20:
                    import shutil
                    shutil.rmtree(IMAGE_UPLOAD_DIR / "assets" / str(document_id), ignore_errors=True)
                    raise HTTPException(422, "No searchable text was found in this file.")
                connection.execute(
                    "UPDATE documents SET body = ?, page_count = ? WHERE id = ?",
                    (body, page_count, document_id),
                )

            elif extension == ".docx":
                temp_path = IMAGE_UPLOAD_DIR / f"temp_{uuid.uuid4()}{extension}"
                temp_path.write_bytes(payload)
                cursor = connection.execute(
                    """INSERT INTO documents (name, body, source, image_path, approved, uploaded_by, page_count, created_at)
                       VALUES (?, '', 'Uploaded; review required', NULL, 0, ?, NULL, ?)""",
                    (name, user.id, now()),
                )
                document_id = cursor.lastrowid
                body, asset_ids = ingest.ingest_docx(temp_path, document_id, IMAGE_UPLOAD_DIR, connection)
                page_count = None
                if len(body.strip()) < 20:
                    import shutil
                    shutil.rmtree(IMAGE_UPLOAD_DIR / "assets" / str(document_id), ignore_errors=True)
                    raise HTTPException(422, "No searchable text was found in this file.")
                connection.execute(
                    "UPDATE documents SET body = ? WHERE id = ?",
                    (body, document_id),
                )

            else:  # TXT, MD
                body = payload.decode("utf-8-sig")
                if len(body.strip()) < 20:
                    raise HTTPException(422, "No searchable text was found in this file.")
                cursor = connection.execute(
                    """INSERT INTO documents (name, body, source, image_path, approved, uploaded_by, page_count, created_at)
                       VALUES (?, ?, 'Uploaded; review required', NULL, 0, ?, NULL, ?)""",
                    (name, body, user.id, now()),
                )
                document_id = cursor.lastrowid
                asset_ids = []
                page_count = None

    except HTTPException:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        raise HTTPException(422, "Could not read this document. Check that it is a valid file.") from exc
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)

    return {
        "id": document_id,
        "name": name,
        "image_path": saved_img_path.name if saved_img_path else None,
        "approved": False,
        "rejected": False,
        "status": "Pending human review",
        "characters": len(body),
        "page_count": page_count,
        "asset_count": len(asset_ids),
    }


@app.post("/api/documents/{document_id}/approve")
def approve_document(
    document_id: int,
    admin: auth.User = Depends(require_admin),
) -> dict[str, Any]:
    """Approve a document (admin only)."""
    with connect() as connection:
        result = connection.execute(
            "UPDATE documents SET approved = 1, rejected = 0, source = 'Human-reviewed upload' WHERE id = ?",
            (document_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Document not found.")
        row = connection.execute(
            "SELECT id, name, source, approved, rejected FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    return dict(row)


@app.post("/api/documents/{document_id}/reject")
def reject_document(
    document_id: int,
    admin: auth.User = Depends(require_admin),
) -> dict[str, Any]:
    """Reject a document (admin only)."""
    with connect() as connection:
        result = connection.execute(
            "UPDATE documents SET approved = 0, rejected = 1 WHERE id = ?",
            (document_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Document not found.")
        row = connection.execute(
            "SELECT id, name, approved, rejected FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
    return dict(row)


@app.delete("/api/documents/{document_id}")
def delete_document(
    document_id: int,
    admin: auth.User = Depends(require_admin),
) -> dict[str, str]:
    """Delete a document and its assets (admin only)."""
    with connect() as connection:
        # Get document info
        doc = connection.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if not doc:
            raise HTTPException(404, "Document not found.")

        # Null queries.document_id
        connection.execute(
            "UPDATE queries SET document_id = NULL WHERE document_id = ?",
            (document_id,),
        )

        # Get and delete asset files
        assets = connection.execute(
            "SELECT file_path FROM document_assets WHERE document_id = ?",
            (document_id,),
        ).fetchall()
        for asset in assets:
            try:
                Path(asset["file_path"]).unlink(missing_ok=True)
            except Exception:
                pass

        if doc["image_path"]:
            (IMAGE_UPLOAD_DIR / doc["image_path"]).unlink(missing_ok=True)
        import shutil
        shutil.rmtree(IMAGE_UPLOAD_DIR / "assets" / str(document_id), ignore_errors=True)

        # Delete document (cascades to assets)
        connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))

    return {"message": "Document deleted"}


@app.post("/api/chat")
async def chat(
    request: ChatRequest,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    """Chat endpoint with SOP generation for procedure requests."""
    query = request.query.strip()
    lang_name = normalize_language(request.language)

    # Get or use shared session
    session_id = request.session_id
    if not session_id:
        with connect() as conn:
            room = conn.execute("SELECT id FROM rooms WHERE name = 'Shared floor'").fetchone()
            if not room:
                raise HTTPException(500, "Default room not found")

            session = conn.execute(
                "SELECT id FROM sessions WHERE kind = 'text' AND room_id = ?",
                (room["id"],),
            ).fetchone()
            if not session:
                session_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO sessions (id, kind, room_id, created_by, created_at, updated_at) "
                    "VALUES (?, 'text', ?, ?, ?, ?)",
                    (session_id, room["id"], user.id, now(), now()),
                )
            else:
                session_id = session["id"]
    else:
        with connect() as conn:
            sess_row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if sess_row:
                if not request.machine_name and sess_row["machine_id"]:
                    request.machine_name = sess_row["machine_id"]
                if not request.machine_type and sess_row["machine_type"]:
                    request.machine_type = sess_row["machine_type"]
                if not request.error_code and sess_row["error_code"]:
                    request.error_code = sess_row["error_code"]
                if not request.module_name and sess_row["module_name"]:
                    request.module_name = sess_row["module_name"]

                conn.execute(
                    "INSERT OR IGNORE INTO session_members (session_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
                    (session_id, user.id, now()),
                )
                conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now(), session_id))

    log_session_message(session_id, "user", query, "text", user_id=user.id)

    # Check if this is a procedure request with enough evidence for SOP
    is_procedure_request = (
        bool(request.generate_sop)
        or (
            (bool(INSTRUCTION_REQUEST.search(query)) or bool(PROCEDURE_REQUEST.search(query)) or is_operational_request(query))
            and not bool(STRONG_OPERATIONAL_INTENT.search(query))
            and not bool(GREETING_PATTERN.search(query))
            and not bool(KB_LOOKUP_QUESTION.search(query))
        )
    )

    with connect() as connection:
        if request.module_name:
            mod_rows = connection.execute(
                "SELECT * FROM documents WHERE approved = 1 AND rejected = 0 AND (name = ? OR name LIKE ?)",
                (request.module_name, f"%{request.module_name}%"),
            ).fetchall()
            rows = mod_rows if mod_rows else connection.execute("SELECT * FROM documents WHERE approved = 1 AND rejected = 0").fetchall()
        elif request.document_id is not None:
            doc_rows = connection.execute(
                "SELECT * FROM documents WHERE approved = 1 AND rejected = 0 AND id = ?",
                (request.document_id,),
            ).fetchall()
            rows = doc_rows if doc_rows else connection.execute("SELECT * FROM documents WHERE approved = 1 AND rejected = 0").fetchall()
        else:
            rows = connection.execute("SELECT * FROM documents WHERE approved = 1 AND rejected = 0").fetchall()

    # AI decides what to look for in RAG by understanding the question and problem
    ai_intent = await ai_understand_problem_for_rag(
        query,
        machine_name=request.machine_name,
        machine_type=request.machine_type,
        error_code=request.error_code,
        module_name=request.module_name,
        expert_notes=request.expert_notes,
    )

    # For SOP generation, retrieve 12 passages
    limit = 12 if is_procedure_request else 3
    matches = retrieve(query, rows, limit=limit, ai_intent=ai_intent)
    citations = _build_citations(matches[:3])  # Default citations

    # SOP generation branch
    if is_procedure_request and matches:
        top_score = matches[0]["score"]
        top_coverage = matches[0]["coverage"]

        if (top_score >= MIN_RETRIEVAL_SCORE and top_coverage >= MIN_EVIDENCE_COVERAGE) or bool(request.generate_sop):
            qualifying_matches = [
                m for m in matches
                if (m["score"] >= MIN_RETRIEVAL_SCORE and m.get("coverage", 0.0) >= MIN_EVIDENCE_COVERAGE) or bool(request.generate_sop)
            ]

            # Add sibling chunks sharing the same heading from the same document
            all_sop_matches = list(qualifying_matches)
            existing_passages = {m["passage"] for m in all_sop_matches}
            for qm in qualifying_matches:
                heading_match = re.search(r"^(#{1,6}\s+.*)$", qm["passage"], re.MULTILINE)
                if heading_match:
                    heading_str = heading_match.group(1).strip()
                    for r in rows:
                        if r["id"] == qm["document_id"]:
                            for sib_p in split_passages(r["body"]):
                                if heading_str in sib_p and sib_p not in existing_passages:
                                    existing_passages.add(sib_p)
                                    all_sop_matches.append({
                                        "document_id": r["id"],
                                        "document_name": r["name"],
                                        "source": r["source"],
                                        "passage": sib_p,
                                        "score": qm["score"],
                                        "coverage": qm["coverage"],
                                        "matched_terms": qm.get("matched_terms", 0),
                                    })

            citations = _build_citations(all_sop_matches)

            # Query assets and room info using a fresh connection
            with connect() as conn:
                room_row = conn.execute("SELECT room_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
                target_room_id = room_row["room_id"] if (room_row and room_row["room_id"]) else 1
                default_room = conn.execute("SELECT id FROM rooms WHERE name = 'Shared floor'").fetchone()
                if default_room:
                    target_room_id = default_room["id"]

                passage_page_numbers = set()
                used_doc_ids = set()
                for match in all_sop_matches:
                    used_doc_ids.add(match["document_id"])
                    for line in match["passage"].splitlines():
                        if "[[page:" in line:
                            try:
                                page_num = int(line.split("[[page:")[1].split("]]")[0])
                                passage_page_numbers.add(page_num)
                            except (ValueError, IndexError):
                                pass

                query_clauses = []
                params = []
                if passage_page_numbers:
                    placeholders = ",".join("?" * len(passage_page_numbers))
                    query_clauses.append(f"page_number IN ({placeholders})")
                    params.extend(list(passage_page_numbers))
                if used_doc_ids:
                    doc_placeholders = ",".join("?" * len(used_doc_ids))
                    query_clauses.append(f"(document_id IN ({doc_placeholders}) AND kind = 'upload')")
                    params.extend(list(used_doc_ids))

                if query_clauses:
                    where_clause = " OR ".join(query_clauses)
                    asset_rows = conn.execute(
                        f"""SELECT id, document_id, page_number, kind, file_path, caption
                            FROM document_assets
                            WHERE {where_clause}""",
                        params,
                    ).fetchall()
                    assets = [dict(a) for a in asset_rows]
                else:
                    assets = []

            # Build SOP
            f_map = {f"F{i}": a["id"] for i, a in enumerate(assets, start=1)}
            f_map.update({a["id"]: a["id"] for a in assets})
            with connect() as ref_conn:
                prior_references = sop.find_similar_sops(
                    ref_conn,
                    query,
                    request.error_code or "",
                    request.machine_name or "",
                )
                expert_learnings = sop.find_relevant_expert_knowledge(
                    ref_conn,
                    query,
                    request.error_code or "",
                    request.machine_name or "",
                )
            sop_data = await sop.build_sop_from_passages(
                query,
                all_sop_matches,
                assets,
                machine_name=request.machine_name,
                machine_type=request.machine_type,
                error_code=request.error_code,
                expert_notes=request.expert_notes,
                prior_references=prior_references,
                expert_learnings=expert_learnings,
                language=lang_name,
            )

            valid_citation_ids = {c["reference_id"] for c in citations}
            valid_figure_ids = {a["id"] for a in assets}
            sop_data = sop.finalize_sop_answer(sop_data, valid_citation_ids, valid_figure_ids, f_map=f_map)

            # If user explicitly requested Generate SOP and LLM or citation gate abstained,
            # construct an evidence-grounded procedure directly from the retrieved passages
            if sop_data.get("abstain") and request.generate_sop and all_sop_matches:
                sop_data = sop.extract_steps_from_passages(query, all_sop_matches, assets, language=lang_name)
                sop_data = sop.finalize_sop_answer(sop_data, valid_citation_ids, valid_figure_ids, f_map=f_map)

            sop_data = sop.attach_part_visuals(
                sop_data,
                assets,
                passages=all_sop_matches,
                query=query,
                error_code=request.error_code,
            )

            # Store user provided operational context for report generation
            if request.machine_name:
                sop_data["machine_name"] = request.machine_name
            if request.machine_type:
                sop_data["machine_type"] = request.machine_type
            if request.error_code:
                sop_data["error_code"] = request.error_code
                sop_data["machine"] = sop_data.get("machine") or request.machine_name or "Fagor CNC 8055"
                sop_data["source"] = sop_data.get("source") or (
                    all_sop_matches[0].get("document_name", "Approved machine manual")
                    if all_sop_matches else "Approved machine manual"
                )
            if request.expert_notes:
                sop_data["expert_notes"] = request.expert_notes
            if request.module_name:
                sop_data["module_name"] = request.module_name
            elif all_sop_matches:
                sop_data["module_name"] = all_sop_matches[0].get("document_name", "")
            sop_data["rag_strategy"] = ai_intent
            sop_data["citations"] = citations
            sop_data["previous_sop_references"] = prior_references
            sop_data["expert_learnings"] = [
                {
                    "id": entry["id"],
                    "detection": entry["detection"],
                    "solution": entry["solution"],
                    "outcome": entry["outcome"],
                    "similarity": entry["similarity"],
                }
                for entry in expert_learnings
            ]

            if not sop_data.get("abstain"):
                with connect() as conn:
                    message_id_row = conn.execute(
                        "SELECT id FROM session_messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                    msg_id = message_id_row["id"] if message_id_row else 0

                    sop_id = sop.create_sop(
                        conn,
                        target_room_id,
                        session_id,
                        msg_id,
                        user.id,
                        query,
                        sop_data,
                    )

                    if request.expert_notes:
                        sop.add_note(conn, sop_id, user.id, "manual", request.expert_notes)

                    # Pre-generate the 5-page dark executive report PDF immediately
                    try:
                        sop.generate_sop_pdf(conn, sop_id, ROOT / "data" / "exports")
                    except Exception as exc:
                        logger.warning(f"Could not pre-generate SOP PDF: {exc}")

                    if msg_id:
                        conn.execute(
                            "UPDATE session_messages SET sop_id = ? WHERE id = ?",
                            (sop_id, msg_id),
                        )

                sop_response = {
                    "id": sop_id,
                    "title": sop_data["title"],
                    "problem": sop_data["problem"],
                    "steps": sop_data["steps"],
                    "solution_steps": sop_data["steps"],
                    "checks": sop_data["checks"],
                    "category": sop_data.get("category", ""),
                    "symptoms": sop_data.get("symptoms", []),
                    "possible_causes": sop_data.get("possible_causes", []),
                    "severity": sop_data.get("severity", ""),
                    "machine": sop_data.get("machine") or sop_data.get("machine_name"),
                    "source": sop_data.get("source") or sop_data.get("module_name"),
                    "related_parts": [
                        {
                            **part,
                            "part_image": part.get("part_image") or (
                                f"/api/assets/{part['part_asset_id']}"
                                if part.get("part_asset_id") else ""
                            ),
                        }
                        for part in sop_data.get("related_parts", [])
                    ],
                    "figures": [
                        {
                            "id": a["id"],
                            "caption": a["caption"],
                            "page": a["page_number"],
                            "url": f"/api/assets/{a['id']}",
                        }
                        for a in assets
                        if a["id"] in valid_figure_ids
                    ],
                    "status": "draft",
                    "export_url": f"/api/sops/{sop_id}/export",
                    "machine_name": sop_data.get("machine_name"),
                    "machine_type": sop_data.get("machine_type"),
                    "error_code": sop_data.get("error_code"),
                    "module_name": sop_data.get("module_name"),
                    "rag_strategy": ai_intent,
                    "previous_sop_references": sop_data.get("previous_sop_references", []),
                }

                rendered_steps = "\n".join(
                    f"{s['number']}. {s['instruction']}"
                    + (f" [{' ,'.join(s['citation_ids'])}]" if s.get('citation_ids') else "")
                    for s in sop_data["steps"]
                )
                response = {
                    "grounded": True,
                    "abstained": False,
                    "answer": rendered_steps,
                    "citations": citations,
                    "retrieval_score": top_score,
                    "sop": sop_response,
                    "rag_strategy": ai_intent,
                    "session_id": session_id,
                }
                log_session_message(session_id, "assistant", rendered_steps, "text", user_id=user.id, sop_id=sop_id)
                return response

    # Operational refusal check (refusal for strong operational, regular answer otherwise)
    if is_operational_request(query) and not bool(request.generate_sop):
        refusal_text = OPERATIONAL_REFUSAL_REPLIES.get(lang_name, OPERATIONAL_REFUSAL_REPLIES["English"])
        response = {
            "grounded": False,
            "abstained": True,
            "reason": "Operational or safety-critical intent requires an approved site procedure and qualified human review.",
            "answer": refusal_text,
            "citations": [],
            "retrieval_score": 0.0,
            "rag_strategy": ai_intent,
            "session_id": session_id,
            "language": lang_name,
        }
        log_query(query, False, None, 0.0, mode="refusal")
        log_session_message(session_id, "assistant", response["answer"], "text", user_id=user.id)
        return response

    # Check if query is indirect or lacks required diagnostic evidence
    if is_indirect_query(query, request.machine_name, request.error_code, request.document_id):
        clarification_text = CLARIFICATION_PROMPTS.get(lang_name, CLARIFICATION_PROMPTS["English"])
        log_query(query, False, None, 0.0, mode="clarification")
        response = {
            "grounded": False,
            "abstained": False,
            "needs_clarification": True,
            "reason": "Query is indirect or lacks required diagnostic evidence (machine model, sensory observations, or error code).",
            "answer": clarification_text,
            "citations": [],
            "retrieval_score": 0.0,
            "rag_strategy": ai_intent,
            "session_id": session_id,
            "language": lang_name,
        }
        log_session_message(session_id, "assistant", clarification_text, "text", user_id=user.id)
        return response

    is_greeting = bool(GREETING_PATTERN.search(query))

    if is_greeting:
        answer, generation_mode = await generate_answer(
            query, citations, image=request.image, audio=request.audio, conversational=True, language=lang_name
        )
        top_score = citations[0]["score"] if citations else 0.0
        top_doc = citations[0]["document_id"] if citations else None
        log_query(query, True, top_doc, top_score, mode="conversational")
        response = {
            "grounded": True,
            "abstained": False,
            "answer": answer,
            "generation_mode": generation_mode,
            "citations": citations,
            "retrieval_score": top_score,
            "rag_strategy": ai_intent,
            "session_id": session_id,
            "language": lang_name,
        }
        log_session_message(session_id, "assistant", answer, "text", user_id=user.id)
        return response

    has_media = bool(request.image or request.audio)
    low_evidence = (
        not citations
        or citations[0]["coverage"] < MIN_EVIDENCE_COVERAGE
        or citations[0]["score"] < MIN_RETRIEVAL_SCORE
    )
    if low_evidence and not has_media:
        insufficient_text = INSUFFICIENT_EVIDENCE_REPLIES.get(lang_name, INSUFFICIENT_EVIDENCE_REPLIES["English"])
        log_query(
            query, False, None, citations[0]["score"] if citations else 0.0, mode="abstained"
        )
        response = {
            "grounded": False,
            "abstained": True,
            "reason": "No sufficiently relevant passage was found in the approved documents.",
            "answer": insufficient_text,
            "citations": [],
            "retrieval_score": citations[0]["score"] if citations else 0.0,
            "rag_strategy": ai_intent,
            "session_id": session_id,
            "language": lang_name,
        }
        log_session_message(session_id, "assistant", response["answer"], "text", user_id=user.id)
        return response

    top_score = citations[0]["score"] if citations else 0.0
    top_doc = citations[0]["document_id"] if citations else None
    answer, generation_mode = await generate_answer(
        query, citations, image=request.image, audio=request.audio, language=lang_name
    )
    log_query(query, True, top_doc, top_score)
    response = {
        "grounded": True,
        "abstained": False,
        "answer": answer,
        "generation_mode": generation_mode,
        "citations": citations,
        "retrieval_score": top_score,
        "rag_strategy": ai_intent,
        "session_id": session_id,
        "language": lang_name,
    }
    log_session_message(session_id, "assistant", answer, "text", user_id=user.id)
    return response


@app.post("/api/detective/sessions", status_code=201)
def start_detective_session(
    request: detective.DetectiveStartRequest,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    """Start a manual-grounded Smart Fault Detective interview."""
    return detective.start_session(user.id, request)


@app.get("/api/detective/sessions/{session_id}")
def get_detective_session(
    session_id: str,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    return detective.get_session(user.id, session_id)


@app.post("/api/detective/sessions/{session_id}/answer")
def answer_detective_question(
    session_id: str,
    request: detective.DetectiveAnswerRequest,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    return detective.answer_session(user.id, session_id, request)


@app.get("/api/analytics")
def analytics(user: auth.User = Depends(require_auth)) -> dict[str, Any]:
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


# ---- Shared room endpoints ----

@app.get("/api/rooms/shared")
def get_shared_room(user: auth.User = Depends(require_auth)) -> dict[str, Any]:
    """Get the shared room info and members."""
    with connect() as connection:
        room = connection.execute("SELECT * FROM rooms WHERE name = 'Shared floor'").fetchone()
        if not room:
            raise HTTPException(404, "Shared room not found")

        members = auth.get_room_members(connection, room["id"])
        session = connection.execute(
            "SELECT id FROM sessions WHERE kind = 'text' AND room_id = ?",
            (room["id"],),
        ).fetchone()

    return {
        "room": dict(room),
        "members": [dict(m) for m in members],
        "session_id": session["id"] if session else None,
    }


@app.get("/api/rooms/shared/messages")
def get_shared_messages(
    after_id: int = 0,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    """Get messages from the shared room after a given ID."""
    with connect() as connection:
        room = connection.execute("SELECT id FROM rooms WHERE name = 'Shared floor'").fetchone()
        if not room:
            raise HTTPException(404, "Shared room not found")

        session = connection.execute(
            "SELECT id FROM sessions WHERE kind = 'text' AND room_id = ?",
            (room["id"],),
        ).fetchone()
        if not session:
            return {"messages": []}

        messages = connection.execute(
            """SELECT sm.id, sm.role, sm.content, sm.modality, sm.sop_id,
                      u.display_name, u.role as user_role, sm.created_at
               FROM session_messages sm
               LEFT JOIN users u ON sm.user_id = u.id
               WHERE sm.session_id = ? AND sm.id > ?
               ORDER BY sm.id""",
            (session["id"], after_id),
        ).fetchall()

    return {
        "messages": [
            {
                "id": m["id"],
                "role": m["role"],
                "content": m["content"],
                "modality": m["modality"],
                "sop_id": m["sop_id"],
                "display_name": m["display_name"] or "System",
                "user_role": m["user_role"],
                "created_at": m["created_at"],
            }
            for m in messages
        ]
    }


# ---- Dedicated Chat Sessions & Multi-Member History Endpoints ----

@app.get("/api/chat/roster")
def get_chat_roster(user: auth.User = Depends(require_auth)) -> dict[str, Any]:
    """Get active registered plant engineers and supervisors to invite to chats."""
    with connect() as conn:
        users = conn.execute(
            "SELECT id, username, display_name, role FROM users WHERE disabled = 0 ORDER BY display_name"
        ).fetchall()
    return {"users": [dict(u) for u in users]}


@app.post("/api/chat/sessions", status_code=201)
def create_chat_session(
    request: CreateChatSessionRequest,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    """Create a separate dedicated troubleshooting chat with machine id, problem name, and members."""
    session_id = str(uuid.uuid4())
    created_ts = now()

    # Determine default name if not provided
    name = (request.name or "").strip()
    if not name:
        if request.machine_id and request.problem:
            name = f"{request.machine_id}: {request.problem[:40]}"
        elif request.machine_id:
            name = f"Workorder {request.machine_id}"
        elif request.problem:
            name = f"Troubleshooting: {request.problem[:40]}"
        else:
            name = f"Session {session_id[:8]}"

    with connect() as conn:
        conn.execute(
            """INSERT INTO sessions (id, kind, room_id, created_by, created_at, updated_at,
                                     machine_id, error_code, error_image, problem_title, name, module_name, status)
               VALUES (?, 'text', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
            (
                session_id,
                user.id,
                created_ts,
                created_ts,
                request.machine_id,
                request.error_code,
                request.error_image,
                request.problem,
                name,
                request.module_name,
            ),
        )

        conn.execute(
            "INSERT OR IGNORE INTO session_members (session_id, user_id, role, joined_at) VALUES (?, ?, 'owner', ?)",
            (session_id, user.id, created_ts),
        )

        invited_names = []
        if request.member_ids:
            for mid in request.member_ids:
                if mid != user.id:
                    u_row = conn.execute("SELECT id, display_name FROM users WHERE id = ?", (mid,)).fetchone()
                    if u_row:
                        conn.execute(
                            "INSERT OR IGNORE INTO session_members (session_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
                            (session_id, mid, created_ts),
                        )
                        invited_names.append(u_row["display_name"])

        sys_details = [f"Machine: {request.machine_id or 'General Plant'}"]
        if request.error_code:
            sys_details.append(f"Error code: {request.error_code}")
        if request.problem:
            sys_details.append(f"Problem: {request.problem}")
        if request.module_name:
            sys_details.append(f"Manual: {request.module_name}")
        if invited_names:
            sys_details.append(f"Collaborators: {', '.join(invited_names)}")

        init_msg = f"Troubleshooting session initialized by {user.display_name}. " + " | ".join(sys_details)
        conn.execute(
            "INSERT INTO session_messages (session_id, role, content, modality, created_at, user_id) VALUES (?, 'system', ?, 'text', ?, ?)",
            (session_id, init_msg, created_ts, user.id),
        )

        members = conn.execute(
            """SELECT u.id, u.username, u.display_name, u.role as user_role, sm.role as session_role
               FROM session_members sm
               JOIN users u ON sm.user_id = u.id
               WHERE sm.session_id = ?
               ORDER BY sm.role DESC, u.display_name ASC""",
            (session_id,),
        ).fetchall()

    return {
        "session_id": session_id,
        "name": name,
        "machine_id": request.machine_id,
        "error_code": request.error_code,
        "error_image": request.error_image,
        "problem_title": request.problem,
        "module_name": request.module_name,
        "created_at": created_ts,
        "updated_at": created_ts,
        "members": [dict(m) for m in members],
    }


@app.get("/api/chat/sessions")
def list_chat_sessions(user: auth.User = Depends(require_auth)) -> dict[str, Any]:
    """List all recorded chat sessions accessible to the user, with machine details and member info."""
    with connect() as conn:
        if user.role == "admin":
            query = """
                SELECT DISTINCT s.id, s.name, s.machine_id, s.error_code, s.error_image, s.problem_title,
                                s.module_name, s.status, s.created_at, s.updated_at, s.room_id,
                                s.created_by, u.display_name as creator_name
                FROM sessions s
                LEFT JOIN users u ON s.created_by = u.id
                WHERE s.kind = 'text'
                ORDER BY s.updated_at DESC
            """
            rows = conn.execute(query).fetchall()
        else:
            query = """
                SELECT DISTINCT s.id, s.name, s.machine_id, s.error_code, s.error_image, s.problem_title,
                                s.module_name, s.status, s.created_at, s.updated_at, s.room_id,
                                s.created_by, u.display_name as creator_name
                FROM sessions s
                LEFT JOIN users u ON s.created_by = u.id
                LEFT JOIN session_members sm ON s.id = sm.session_id
                WHERE s.kind = 'text' AND (sm.user_id = ? OR s.created_by = ? OR s.room_id IS NOT NULL)
                ORDER BY s.updated_at DESC
            """
            rows = conn.execute(query, (user.id, user.id)).fetchall()

        results = []
        for r in rows:
            sid = r["id"]
            msg_count = conn.execute(
                "SELECT COUNT(*) FROM session_messages WHERE session_id = ?", (sid,)
            ).fetchone()[0]

            last_msg = conn.execute(
                """SELECT content, role, created_at FROM session_messages
                   WHERE session_id = ? ORDER BY id DESC LIMIT 1""",
                (sid,),
            ).fetchone()

            members = conn.execute(
                """SELECT u.id, u.username, u.display_name, u.role as user_role, sm.role as session_role
                   FROM session_members sm
                   JOIN users u ON sm.user_id = u.id
                   WHERE sm.session_id = ?""",
                (sid,),
            ).fetchall()

            display_name = r["name"]
            if not display_name:
                if r["machine_id"] and r["problem_title"]:
                    display_name = f"{r['machine_id']}: {r['problem_title'][:40]}"
                elif r["machine_id"]:
                    display_name = f"Workorder {r['machine_id']}"
                elif r["room_id"] is not None:
                    display_name = "Plant Floor Shared Stream"
                else:
                    display_name = f"Session {sid[:8]}"

            results.append({
                "id": sid,
                "name": display_name,
                "machine_id": r["machine_id"],
                "error_code": r["error_code"],
                "error_image": r["error_image"],
                "problem_title": r["problem_title"],
                "module_name": r["module_name"],
                "status": r["status"] or "active",
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "is_shared_floor": bool(r["room_id"] is not None),
                "creator_name": r["creator_name"] or "System",
                "message_count": msg_count,
                "last_message": {
                    "content": last_msg["content"][:100] if last_msg else "",
                    "role": last_msg["role"] if last_msg else "",
                    "created_at": last_msg["created_at"] if last_msg else r["created_at"],
                } if last_msg else None,
                "members": [dict(m) for m in members],
            })

    return {"sessions": results}


@app.get("/api/chat/sessions/{session_id}")
def get_chat_session(
    session_id: str,
    after_id: int = 0,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    """Resume chatting and get complete saved chat history and attached SOP cards."""
    with connect() as conn:
        session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(404, "Chat session not found")

        members = conn.execute(
            """SELECT u.id, u.username, u.display_name, u.role as user_role, sm.role as session_role
               FROM session_members sm
               JOIN users u ON sm.user_id = u.id
               WHERE sm.session_id = ?
               ORDER BY sm.role DESC, u.display_name ASC""",
            (session_id,),
        ).fetchall()

        raw_msgs = conn.execute(
            """SELECT sm.id, sm.role, sm.content, sm.modality, sm.sop_id,
                      u.display_name, u.role as user_role, sm.created_at
               FROM session_messages sm
               LEFT JOIN users u ON sm.user_id = u.id
               WHERE sm.session_id = ? AND sm.id > ?
               ORDER BY sm.id ASC""",
            (session_id, after_id),
        ).fetchall()

        messages = []
        for m in raw_msgs:
            msg_dict = {
                "id": m["id"],
                "role": m["role"],
                "content": m["content"],
                "modality": m["modality"],
                "sop_id": m["sop_id"],
                "display_name": m["display_name"] or ("System" if m["role"] == "system" else "AI Assistant"),
                "user_role": m["user_role"],
                "created_at": m["created_at"],
            }
            if m["sop_id"]:
                sop_row = conn.execute("SELECT * FROM sops WHERE id = ?", (m["sop_id"],)).fetchone()
                if sop_row:
                    try:
                        ans_data = json.loads(sop_row["answer_json"])
                        msg_dict["sop"] = {
                            "id": sop_row["id"],
                            "title": ans_data.get("title", f"SOP-{sop_row['id']}"),
                            "problem": ans_data.get("problem", ""),
                            "steps": ans_data.get("steps", []),
                            "checks": ans_data.get("checks", []),
                            "status": sop_row["status"],
                            "export_url": f"/api/sops/{sop_row['id']}/export",
                            "machine_name": ans_data.get("machine_name"),
                            "machine_type": ans_data.get("machine_type"),
                            "error_code": ans_data.get("error_code"),
                            "module_name": ans_data.get("module_name"),
                            "rag_strategy": ans_data.get("rag_strategy"),
                        }
                    except Exception as e:
                        logger.warning("Could not hydrate SOP %s: %s", m["sop_id"], e)
            messages.append(msg_dict)

        name = session["name"]
        if not name:
            if session["machine_id"] and session["problem_title"]:
                name = f"{session['machine_id']}: {session['problem_title'][:40]}"
            elif session["machine_id"]:
                name = f"Workorder {session['machine_id']}"
            elif session["room_id"] is not None:
                name = "Plant Floor Shared Stream"
            else:
                name = f"Session {session_id[:8]}"

    return {
        "session": {
            "id": session["id"],
            "name": name,
            "machine_id": session["machine_id"],
            "error_code": session["error_code"],
            "error_image": session["error_image"],
            "problem_title": session["problem_title"],
            "module_name": session["module_name"],
            "status": session["status"] or "active",
            "created_at": session["created_at"],
            "updated_at": session["updated_at"],
            "is_shared_floor": bool(session["room_id"] is not None),
        },
        "members": [dict(m) for m in members],
        "messages": messages,
    }


@app.get("/api/chat/sessions/{session_id}/knowledge-export")
def export_chat_knowledge_pdf(
    session_id: str,
    user: auth.User = Depends(require_auth),
) -> FileResponse:
    """Export a structured problem, detection, expert solution, and SOP reference PDF."""
    with connect() as connection:
        session = connection.execute(
            """SELECT id, machine_id, error_code, problem_title, name
               FROM sessions WHERE id = ?""",
            (session_id,),
        ).fetchone()
        if not session:
            raise HTTPException(404, "Chat session not found")
        room_id = connection.execute(
            "SELECT room_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()["room_id"]
        if room_id is None:
            room_id = connection.execute("SELECT id FROM rooms ORDER BY id ASC LIMIT 1").fetchone()["id"]
        if not auth.is_user_in_room(connection, user.id, room_id):
            raise HTTPException(403, "You must be a room member")

        messages = [
            dict(row) for row in connection.execute(
                """SELECT sm.role, sm.content, sm.created_at, u.display_name
                   FROM session_messages sm
                   LEFT JOIN users u ON u.id = sm.user_id
                   WHERE sm.session_id = ?
                   ORDER BY sm.id""",
                (session_id,),
            ).fetchall()
        ]
        expert_entries = sop.list_expert_knowledge(connection, session_id=session_id)
        references = sop.find_similar_sops(
            connection,
            session["problem_title"] or "",
            session["error_code"] or "",
            session["machine_id"] or "",
        )

    from backend import report_generator
    export_path = ROOT / "data" / "exports" / f"MechTechAI_Expert_Knowledge_{session_id[:8]}.pdf"
    report_generator.generate_chat_knowledge_report(
        export_path,
        dict(session),
        messages,
        expert_entries,
        references,
    )
    return FileResponse(export_path, media_type="application/pdf", filename=export_path.name)


@app.post("/api/chat/sessions/{session_id}/members")
def add_chat_member(
    session_id: str,
    request: AddChatMemberRequest,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    """Add an engineer or supervisor to an existing troubleshooting chat."""
    with connect() as conn:
        session = conn.execute("SELECT id, name FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(404, "Chat session not found")

        target_user = None
        if request.user_id:
            target_user = conn.execute("SELECT id, username, display_name FROM users WHERE id = ?", (request.user_id,)).fetchone()
        elif request.username:
            target_user = conn.execute("SELECT id, username, display_name FROM users WHERE username = ?", (request.username,)).fetchone()

        if not target_user:
            raise HTTPException(404, "User not found")

        now_ts = now()
        conn.execute(
            "INSERT OR IGNORE INTO session_members (session_id, user_id, role, joined_at) VALUES (?, ?, 'member', ?)",
            (session_id, target_user["id"], now_ts),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now_ts, session_id))

        sys_msg = f"👥 {target_user['display_name']} was added to this troubleshooting session by {user.display_name}."
        conn.execute(
            "INSERT INTO session_messages (session_id, role, content, modality, created_at, user_id) VALUES (?, 'system', ?, 'text', ?, ?)",
            (session_id, sys_msg, now_ts, user.id),
        )

        members = conn.execute(
            """SELECT u.id, u.username, u.display_name, u.role as user_role, sm.role as session_role
               FROM session_members sm
               JOIN users u ON sm.user_id = u.id
               WHERE sm.session_id = ?
               ORDER BY sm.role DESC, u.display_name ASC""",
            (session_id,),
        ).fetchall()

    return {
        "message": f"{target_user['display_name']} added successfully",
        "members": [dict(m) for m in members],
    }


@app.delete("/api/chat/sessions/{session_id}/members/{target_user_id}")
def remove_chat_member(
    session_id: str,
    target_user_id: int,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    """Remove a member from a troubleshooting chat session."""
    with connect() as conn:
        session = conn.execute("SELECT id, created_by FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            raise HTTPException(404, "Chat session not found")

        if user.id != target_user_id and user.id != session["created_by"] and user.role != "admin":
            raise HTTPException(403, "Not authorized to remove members from this session")

        target = conn.execute("SELECT display_name FROM users WHERE id = ?", (target_user_id,)).fetchone()
        conn.execute("DELETE FROM session_members WHERE session_id = ? AND user_id = ?", (session_id, target_user_id))
        now_ts = now()
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now_ts, session_id))

        if target:
            sys_msg = f"{target['display_name']} left or was removed from this session."
            conn.execute(
                "INSERT INTO session_messages (session_id, role, content, modality, created_at, user_id) VALUES (?, 'system', ?, 'text', ?, ?)",
                (session_id, sys_msg, now_ts, user.id),
            )

    return {"message": "Member removed"}


# ---- Invite endpoints ----

@app.post("/api/invites", status_code=201)
def create_invite(
    display_hint: str = "",
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    """Create an invite code."""
    with connect() as connection:
        room = connection.execute("SELECT id FROM rooms WHERE name = 'Shared floor'").fetchone()
        if not room:
            raise HTTPException(404, "Shared room not found")

        # Check if user is a room member
        if not auth.is_user_in_room(connection, user.id, room["id"]):
            raise HTTPException(403, "You must be a room member to create invites")

        code, expires_at = auth.create_invite(connection, user.id, room["id"])

    return {
        "code": code,
        "url": f"/invite/{code}",
        "expires_at": expires_at,
    }


@app.get("/api/invites/{code}")
def get_invite_info(code: str) -> dict[str, Any]:
    """Check if an invite code is valid."""
    with connect() as connection:
        if auth.is_invite_valid(connection, code):
            return {"valid": True}
        else:
            return {"valid": False}


@app.post("/api/invites/{code}/accept", status_code=201)
def accept_invite(
    code: str,
    request: auth.AcceptInviteRequest,
    response: Response,
    http_request: Request,
) -> dict[str, Any]:
    """Accept an invite and create a new engineer account."""
    with connect() as connection:
        user_id = auth.accept_invite(
            connection, code, request.username, request.display_name, request.password
        )

        # Create session
        session_token, expires_at = auth.create_session(connection, user_id)

    # Set cookie
    response.set_cookie(
        key=auth.SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=is_cookie_secure(http_request),
        max_age=auth.SESSION_EXPIRY_DAYS * 24 * 60 * 60,
    )

    return {"message": "Account created", "user_id": user_id}


# ---- SOP endpoints ----

@app.get("/api/sops/{sop_id}")
def get_sop(sop_id: int, user: auth.User = Depends(require_auth)) -> dict[str, Any]:
    """Get an SOP with feedback and notes."""
    with connect() as connection:
        sop_row = connection.execute(
            """SELECT s.*, r.name as room_name, u.display_name as author_name
               FROM sops s
               JOIN rooms r ON s.room_id = r.id
               JOIN users u ON s.author_id = u.id
               WHERE s.id = ?""",
            (sop_id,),
        ).fetchone()
        if not sop_row:
            raise HTTPException(404, "SOP not found")

        # Check room membership
        if not auth.is_user_in_room(connection, user.id, sop_row["room_id"]):
            raise HTTPException(403, "You must be a room member to view this SOP")

        answer_json = json.loads(sop_row["answer_json"])

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

        # Collect figures
        step_fig_ids = [fid for step in answer_json.get("steps", []) for fid in step.get("figure_ids", [])]
        part_fig_ids = [
            part.get("part_asset_id")
            for part in answer_json.get("related_parts", [])
            if part.get("part_asset_id") is not None
        ]
        step_fig_ids = list(dict.fromkeys(step_fig_ids + part_fig_ids))
        figures = []
        if step_fig_ids:
            placeholders = ",".join("?" * len(step_fig_ids))
            asset_rows = connection.execute(
                f"""SELECT id, caption, page_number
                   FROM document_assets
                   WHERE id IN ({placeholders})""",
                step_fig_ids,
            ).fetchall()
            figures = [
                {
                    "id": a["id"],
                    "caption": a["caption"],
                    "page": a["page_number"],
                    "url": f"/api/assets/{a['id']}",
                }
                for a in asset_rows
            ]

    return {
        "id": sop_row["id"],
        "title": answer_json.get("title", ""),
        "problem": answer_json.get("problem", ""),
        "steps": answer_json.get("steps", []),
        "solution_steps": answer_json.get("solution_steps", answer_json.get("steps", [])),
        "checks": answer_json.get("checks", []),
        "category": answer_json.get("category", ""),
        "symptoms": answer_json.get("symptoms", []),
        "possible_causes": answer_json.get("possible_causes", []),
        "severity": answer_json.get("severity", ""),
        "machine": answer_json.get("machine") or answer_json.get("machine_name"),
        "source": answer_json.get("source") or answer_json.get("module_name"),
        "related_parts": answer_json.get("related_parts", []),
        "status": sop_row["status"],
        "author": sop_row["author_name"],
        "created_at": sop_row["created_at"],
        "figures": figures,
        "feedback": [dict(f) for f in feedback],
        "notes": [dict(n) for n in notes],
        "machine_name": answer_json.get("machine_name"),
        "machine_type": answer_json.get("machine_type"),
        "error_code": answer_json.get("error_code"),
        "module_name": answer_json.get("module_name"),
        "expert_notes": answer_json.get("expert_notes"),
        "export_url": f"/api/sops/{sop_row['id']}/export",
        "rag_strategy": answer_json.get("rag_strategy"),
    }


@app.post("/api/sops/{sop_id}/feedback")
def add_sop_feedback(
    sop_id: int,
    request: sop.FeedbackRequest,
    user: auth.User = Depends(require_auth),
) -> dict[str, str]:
    """Add feedback to an SOP."""
    with connect() as connection:
        sop_row = connection.execute(
            "SELECT room_id, session_id, problem, answer_json FROM sops WHERE id = ?",
            (sop_id,),
        ).fetchone()
        if not sop_row:
            raise HTTPException(404, "SOP not found")

        if not auth.is_user_in_room(connection, user.id, sop_row["room_id"]):
            raise HTTPException(403, "You must be a room member")

        sop.add_feedback(
            connection, sop_id, user.id, request.applied_how, request.outcome, request.notes
        )
        answer_data = json.loads(sop_row["answer_json"])
        if request.applied_how.strip() or request.notes.strip():
            sop.add_expert_knowledge(
                connection,
                room_id=sop_row["room_id"],
                session_id=sop_row["session_id"],
                sop_id=sop_id,
                author_id=user.id,
                machine_name=answer_data.get("machine_name") or answer_data.get("machine", ""),
                error_code=answer_data.get("error_code", ""),
                detection=answer_data.get("problem") or sop_row["problem"],
                solution=request.applied_how,
                outcome=request.outcome,
                notes=request.notes,
                approved=request.outcome == "fixed",
            )
        outcome_label = request.outcome.replace("_", " ")
        sys_msg = f"{user.display_name} marked this {outcome_label}"
        log_session_message(sop_row["session_id"], "system", sys_msg, "text", user_id=user.id, sop_id=sop_id, connection=connection)

    return {"message": "Feedback added"}


@app.post("/api/chat/sessions/{session_id}/expert-feedback", status_code=201)
def add_expert_chat_feedback(
    session_id: str,
    request: sop.ExpertFeedbackRequest,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    """Capture a technician's detection and solution as reusable expert knowledge."""
    with connect() as connection:
        session = connection.execute(
            "SELECT id, room_id, machine_id, error_code FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not session:
            raise HTTPException(404, "Chat session not found")
        room_id = session["room_id"]
        if room_id is None:
            room = connection.execute("SELECT id FROM rooms ORDER BY id ASC LIMIT 1").fetchone()
            room_id = room["id"] if room else 1
        if not auth.is_user_in_room(connection, user.id, room_id):
            raise HTTPException(403, "You must be a room member")

        knowledge_id = sop.add_expert_knowledge(
            connection,
            room_id=room_id,
            session_id=session_id,
            sop_id=None,
            author_id=user.id,
            machine_name=session["machine_id"] or "",
            error_code=request.error_code or session["error_code"] or "",
            detection=request.detection,
            solution=request.solution,
            outcome=request.outcome,
            notes=request.notes,
            approved=request.outcome == "fixed",
        )
        log_session_message(
            session_id,
            "system",
            f"{user.display_name} added expert solution learning ({request.outcome}).",
            "text",
            user_id=user.id,
            connection=connection,
        )
    return {"message": "Expert solution learning recorded", "knowledge_id": knowledge_id}


@app.get("/api/chat/sessions/{session_id}/expert-feedback")
def get_expert_chat_feedback(
    session_id: str,
    user: auth.User = Depends(require_auth),
) -> dict[str, Any]:
    with connect() as connection:
        session = connection.execute(
            "SELECT room_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not session:
            raise HTTPException(404, "Chat session not found")
        room_id = session["room_id"]
        if room_id is None:
            room = connection.execute("SELECT id FROM rooms ORDER BY id ASC LIMIT 1").fetchone()
            room_id = room["id"] if room else 1
        if not auth.is_user_in_room(connection, user.id, room_id):
            raise HTTPException(403, "You must be a room member")
        entries = sop.list_expert_knowledge(connection, session_id=session_id)
    return {"knowledge": entries}


@app.post("/api/sops/{sop_id}/notes")
def add_sop_note(
    sop_id: int,
    request: sop.NoteRequest,
    user: auth.User = Depends(require_auth),
) -> dict[str, str]:
    """Add a manual note to an SOP."""
    with connect() as connection:
        sop_row = connection.execute("SELECT room_id, session_id FROM sops WHERE id = ?", (sop_id,)).fetchone()
        if not sop_row:
            raise HTTPException(404, "SOP not found")

        if not auth.is_user_in_room(connection, user.id, sop_row["room_id"]):
            raise HTTPException(403, "You must be a room member")

        sop.add_note(connection, sop_id, user.id, "manual", request.body)
        sys_msg = f"{user.display_name} added an engineer note"
        log_session_message(sop_row["session_id"], "system", sys_msg, "text", user_id=user.id, sop_id=sop_id, connection=connection)

    return {"message": "Note added"}


@app.post("/api/sops/{sop_id}/notes/synthesize")
async def synthesize_sop_note(
    sop_id: int,
    user: auth.User = Depends(require_auth),
) -> dict[str, str]:
    """Generate a synthesized note from feedback and manual notes."""
    with connect() as connection:
        sop_row = connection.execute("SELECT room_id, session_id FROM sops WHERE id = ?", (sop_id,)).fetchone()
        if not sop_row:
            raise HTTPException(404, "SOP not found")

        if not auth.is_user_in_room(connection, user.id, sop_row["room_id"]):
            raise HTTPException(403, "You must be a room member")

        note_text = await sop.synthesize_note(connection, sop_id)
        sop.add_note(connection, sop_id, user.id, "synthesized", note_text)
        sys_msg = f"{user.display_name} synthesized notes from engineers"
        log_session_message(sop_row["session_id"], "system", sys_msg, "text", user_id=user.id, sop_id=sop_id, connection=connection)

    return {"message": "Synthesized note added"}


@app.get("/api/sops/{sop_id}/export")
@app.post("/api/sops/{sop_id}/export")
def export_sop_pdf(
    sop_id: int,
    user: auth.User = Depends(require_auth),
) -> FileResponse:
    """Export an SOP as PDF."""
    with connect() as connection:
        sop_row = connection.execute("SELECT room_id FROM sops WHERE id = ?", (sop_id,)).fetchone()
        if not sop_row:
            raise HTTPException(404, "SOP not found")

        if not auth.is_user_in_room(connection, user.id, sop_row["room_id"]):
            raise HTTPException(403, "You must be a room member")

        export_path = sop.generate_sop_pdf(connection, sop_id, ROOT / "data" / "exports")

    return FileResponse(export_path, media_type="application/pdf", filename=export_path.name)


@app.get("/api/sops/{sop_id}/quick-export")
@app.post("/api/sops/{sop_id}/quick-export")
def export_quick_sop_pdf(
    sop_id: int,
    user: auth.User = Depends(require_auth),
) -> FileResponse:
    """Export a concise black-and-white, source-page-referenced SOP."""
    with connect() as connection:
        sop_row = connection.execute("SELECT room_id FROM sops WHERE id = ?", (sop_id,)).fetchone()
        if not sop_row:
            raise HTTPException(404, "SOP not found")
        if not auth.is_user_in_room(connection, user.id, sop_row["room_id"]):
            raise HTTPException(403, "You must be a room member")
        export_path = sop.generate_sop_pdf(
            connection, sop_id, ROOT / "data" / "exports", compact=True
        )
    return FileResponse(export_path, media_type="application/pdf", filename=export_path.name)


@app.post("/api/sops/{sop_id}/review")
def review_sop(
    sop_id: int,
    admin: auth.User = Depends(require_admin),
) -> dict[str, str]:
    """Mark an SOP as reviewed (admin only)."""
    with connect() as connection:
        result = connection.execute(
            "UPDATE sops SET status = 'reviewed' WHERE id = ?",
            (sop_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "SOP not found")

    return {"message": "SOP marked as reviewed"}


@app.delete("/api/sops/{sop_id}")
def delete_sop(
    sop_id: int,
    admin: auth.User = Depends(require_admin),
) -> dict[str, str]:
    """Delete an SOP (admin only)."""
    with connect() as connection:
        sop_row = connection.execute("SELECT export_path FROM sops WHERE id = ?", (sop_id,)).fetchone()
        if not sop_row:
            raise HTTPException(404, "SOP not found")

        # Delete export file
        if sop_row["export_path"]:
            try:
                Path(sop_row["export_path"]).unlink(missing_ok=True)
            except Exception:
                pass

        # Delete SOP (cascades to feedback and notes)
        connection.execute("DELETE FROM sops WHERE id = ?", (sop_id,))

    return {"message": "SOP deleted"}


# ---- Asset serving ----

@app.get("/api/assets/{asset_id}")
def serve_asset(asset_id: int, user: auth.User = Depends(require_auth)) -> FileResponse:
    """Serve a document asset."""
    with connect() as connection:
        asset = connection.execute(
            "SELECT file_path FROM document_assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
        if not asset:
            raise HTTPException(404, "Asset not found")

    asset_path = Path(asset["file_path"]).resolve()
    base_dir = (IMAGE_UPLOAD_DIR / "assets").resolve()
    try:
        asset_path.relative_to(base_dir)
    except ValueError:
        raise HTTPException(403, "Invalid asset path")

    if not asset_path.exists():
        raise HTTPException(404, "Asset file not found")

    return FileResponse(asset_path)


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


# ---- Model Selection & Diagnostics Endpoints (Admin Only) ----

class ModelConfigRequest(BaseModel):
    provider: str = Field(..., description="'gemini' or 'nvidia'")
    model: Optional[str] = Field(None, description="Model identifier to activate")


class ModelTestRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    prompt: Optional[str] = "Briefly introduce yourself in 2 sentences and render a 3-step test workflow diagram in mermaid format."


@app.get("/api/admin/model-config")
def get_model_config_endpoint(admin: auth.User = Depends(require_admin)) -> dict[str, Any]:
    """Return the current active model provider and available models (Admin only)."""
    return get_active_model_config()


@app.post("/api/admin/model-config")
def set_model_config_endpoint(
    req: ModelConfigRequest,
    admin: auth.User = Depends(require_admin),
) -> dict[str, Any]:
    """Update active model provider and model preference (Admin only)."""
    if req.provider not in ("gemini", "nvidia"):
        raise HTTPException(400, "Provider must be 'gemini' or 'nvidia'")

    set_app_setting("llm_provider", req.provider)
    if req.model:
        if req.provider == "gemini":
            set_app_setting("gemini_model", req.model)
        else:
            set_app_setting("nvidia_model", req.model)

    return {
        "success": True,
        "message": f"Active AI provider updated to {req.provider.upper()}",
        "config": get_active_model_config(),
    }


@app.post("/api/admin/model-test")
async def test_model_endpoint(
    req: ModelTestRequest,
    admin: auth.User = Depends(require_admin),
) -> dict[str, Any]:
    """Test response generation and measure round-trip latency for the selected model (Admin only)."""
    config = get_active_model_config()
    target_provider = req.provider or config["provider"]
    target_model = req.model or (config["gemini_model"] if target_provider == "gemini" else config["nvidia_model"])

    start_time = time.perf_counter()
    try:
        reply = await call_active_llm(
            system_prompt="You are NexusSOP AI. Respond concisely. Include a mini Mermaid flowchart if requested.",
            user_prompt=req.prompt or "Provide a short 2-sentence verification response with a 3-node mermaid flowchart.",
            override_provider=target_provider,
            override_model=target_model,
        )
        latency_ms = round((time.perf_counter() - start_time) * 1000, 1)
        return {
            "success": True,
            "provider": target_provider,
            "model": target_model,
            "latency_ms": latency_ms,
            "reply": reply,
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 1)
        return {
            "success": False,
            "provider": target_provider,
            "model": target_model,
            "latency_ms": latency_ms,
            "error": str(exc),
        }
