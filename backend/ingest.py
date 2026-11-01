"""Document ingestion with figure extraction for NexusSOP.

Handles:
- PDF text extraction with page markers
- PDF embedded image extraction via PyMuPDF
- PDF page snapshots for vector diagrams
- DOCX text and inline image extraction
- Image OCR (already in main.py, reused here)
"""

import io
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from docx import Document
from PIL import Image

# OCR function (duplicated from main.py to avoid circular import)
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


CAPTION_PATTERN = re.compile(
    r"(?:figure|fig\.|diagram|chart|graph|illustration|image)\s*\d*[.:]?\s*",
    re.IGNORECASE,
)


def now() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_pdf_text_with_pages(pdf_path: Path) -> str:
    """Extract text from PDF with page markers [[page:N]].

    Returns the combined text with page markers.
    """
    try:
        doc = fitz.open(pdf_path)
        pages_text = []
        for page_num, page in enumerate(doc, start=1):
            text = page.get_text() or ""
            pages_text.append(f"[[page:{page_num}]]\n{text}")
        doc.close()
        return "\n\n".join(pages_text)
    except Exception:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        pages_text = []
        for page_num, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            pages_text.append(f"[[page:{page_num}]]\n{text}")
        return "\n\n".join(pages_text)


def extract_pdf_images(pdf_path: Path, document_id: int, upload_dir: Path) -> list[dict]:
    """Extract embedded images from PDF using PyMuPDF.

    Returns list of asset dicts with keys:
    - page_number: int
    - kind: 'embedded'
    - file_path: Path
    - caption: str
    """
    doc = fitz.open(pdf_path)
    assets = []
    asset_dir = upload_dir / "assets" / str(document_id)
    asset_dir.mkdir(parents=True, exist_ok=True)
    seen_xrefs = set()
    max_images = 60

    for page_num, page in enumerate(doc, start=1):
        if len(assets) >= max_images:
            break
        image_list = page.get_images(full=True)
        img_index = 0
        for img in image_list:
            xref = img[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)

            try:
                base_image = doc.extract_image(xref)
            except Exception:
                continue
            if not base_image:
                continue

            width = base_image.get("width", 0)
            height = base_image.get("height", 0)
            if width < 16 or height < 16:
                continue

            img_index += 1
            image_bytes = base_image["image"]
            image_ext = base_image.get("ext", "png")

            # Save image
            image_filename = f"page{page_num}_img{img_index}.{image_ext}"
            image_path = asset_dir / image_filename
            image_path.write_bytes(image_bytes)

            # Find caption from page text
            page_text = page.get_text()
            caption = find_caption(page_text, page_num)

            assets.append({
                "page_number": page_num,
                "kind": "embedded",
                "file_path": image_path,
                "caption": caption,
            })
            if len(assets) >= max_images:
                break

    doc.close()
    return assets


def create_pdf_page_snapshot(pdf_path: Path, page_num: int, document_id: int, upload_dir: Path, doc: Optional[fitz.Document] = None) -> Optional[Path]:
    """Create a PNG snapshot of a PDF page for vector diagrams.

    Returns the image path or None if the page has no drawings.
    """
    close_doc = False
    if doc is None:
        doc = fitz.open(pdf_path)
        close_doc = True
    if page_num < 1 or page_num > len(doc):
        if close_doc:
            doc.close()
        return None

    page = doc[page_num - 1]

    # Check if page has drawings (vector content)
    drawings = page.get_drawings()
    if not drawings or len(drawings) < 3:
        if close_doc:
            doc.close()
        return None

    # Render page to PNG
    mat = fitz.Matrix(1.5, 1.5)  # 1.5x zoom for good quality & speed
    pix = page.get_pixmap(matrix=mat)
    image_bytes = pix.tobytes("png")
    pix = None

    asset_dir = upload_dir / "assets" / str(document_id)
    asset_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = asset_dir / f"page{page_num}_snapshot.png"
    snapshot_path.write_bytes(image_bytes)

    if close_doc:
        doc.close()
    return snapshot_path


def find_caption(page_text: str, page_num: int) -> str:
    """Find a caption for an image on a page.

    Looks for patterns like "Figure 1:", "Fig. 2 -", "Diagram showing..."
    """
    lines = page_text.split("\n")
    for line in lines:
        line = line.strip()
        if CAPTION_PATTERN.match(line):
            return line[:200]  # Truncate long captions

    return f"Page {page_num}"


def ingest_pdf(
    pdf_path: Path,
    document_id: int,
    upload_dir: Path,
    connection: sqlite3.Connection,
) -> tuple[str, list[int]]:
    """Ingest a PDF document with text and assets.

    Returns (body_text, asset_ids).
    """
    doc = fitz.open(pdf_path)
    pages_text = []
    asset_ids = []
    asset_dir = upload_dir / "assets" / str(document_id)
    asset_dir.mkdir(parents=True, exist_ok=True)

    seen_xrefs = set()
    max_images = 60
    max_snapshots = 15
    snapshot_count = 0

    for page_num, page in enumerate(doc, start=1):
        text = page.get_text() or ""
        pages_text.append(f"[[page:{page_num}]]\n{text}")

        has_embedded_images = False
        if len(asset_ids) < max_images:
            image_list = page.get_images(full=True)
            img_index = 0
            for img in image_list:
                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)

                try:
                    base_image = doc.extract_image(xref)
                except Exception:
                    continue
                if not base_image:
                    continue

                width = base_image.get("width", 0)
                height = base_image.get("height", 0)
                if width < 16 or height < 16:
                    continue

                img_index += 1
                image_bytes = base_image["image"]
                image_ext = base_image.get("ext", "png")

                image_filename = f"page{page_num}_img{img_index}.{image_ext}"
                image_path = asset_dir / image_filename
                image_path.write_bytes(image_bytes)

                caption = find_caption(text, page_num)
                connection.execute(
                    """INSERT INTO document_assets
                       (document_id, page_number, kind, file_path, caption, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (document_id, page_num, "embedded", str(image_path), caption, now()),
                )
                asset_ids.append(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                has_embedded_images = True
                if len(asset_ids) >= max_images:
                    break

        if not has_embedded_images and snapshot_count < max_snapshots:
            drawings = page.get_drawings()
            if drawings and len(drawings) >= 3:
                mat = fitz.Matrix(1.5, 1.5)
                pix = page.get_pixmap(matrix=mat)
                snapshot_path = asset_dir / f"page{page_num}_snapshot.png"
                snapshot_path.write_bytes(pix.tobytes("png"))
                pix = None
                snapshot_count += 1

                caption = find_caption(text, page_num)
                connection.execute(
                    """INSERT INTO document_assets
                       (document_id, page_number, kind, file_path, caption, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (document_id, page_num, "page", str(snapshot_path), caption, now()),
                )
                asset_ids.append(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    doc.close()
    body = "\n\n".join(pages_text)
    return body, asset_ids


def ingest_docx(
    docx_path: Path,
    document_id: int,
    upload_dir: Path,
    connection: sqlite3.Connection,
) -> tuple[str, list[int]]:
    """Ingest a DOCX document with text and inline images.

    Returns (body_text, asset_ids).
    """
    doc = Document(docx_path)
    body_parts = []
    asset_ids = []
    asset_dir = upload_dir / "assets" / str(document_id)
    asset_dir.mkdir(parents=True, exist_ok=True)

    image_counter = 0

    for paragraph in doc.paragraphs:
        # Check for inline images
        if paragraph.runs:
            for run in paragraph.runs:
                if run._element.xpath(".//pic:pic"):  # Has an image
                    # Extract image
                    for rel in doc.part.rels.values():
                        if "image" in rel.target_ref:
                            try:
                                image_bytes = rel.target_part.blob
                                image_ext = rel.target_ref.split(".")[-1]
                                image_filename = f"inline_{image_counter}.{image_ext}"
                                image_path = asset_dir / image_filename
                                image_path.write_bytes(image_bytes)

                                # Caption from previous paragraph text
                                caption = paragraph.text[:200] or f"Inline image {image_counter}"

                                connection.execute(
                                    """INSERT INTO document_assets
                                       (document_id, page_number, kind, file_path, caption, created_at)
                                       VALUES (?, ?, ?, ?, ?, ?)""",
                                    (document_id, None, "upload", str(image_path), caption, now()),
                                )
                                asset_ids.append(
                                    connection.execute("SELECT last_insert_rowid()").fetchone()[0]
                                )
                                image_counter += 1
                                break
                            except Exception:
                                pass

        body_parts.append(paragraph.text)

    # Handle tables
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text for cell in row.cells)
            body_parts.append(row_text)

    body = "\n\n".join(body_parts)
    return body, asset_ids


def ingest_image(
    image_path: Path,
    document_id: int,
    upload_dir: Path,
    connection: sqlite3.Connection,
) -> tuple[str, list[int]]:
    """Ingest an image document with OCR.

    Returns (body_text, asset_ids).
    """
    # OCR the image
    ocr_text = ocr_image(image_path)

    # Copy image to assets directory
    asset_dir = upload_dir / "assets" / str(document_id)
    asset_dir.mkdir(parents=True, exist_ok=True)

    asset_filename = image_path.name
    asset_path = asset_dir / asset_filename
    import shutil
    shutil.copy(image_path, asset_path)

    # Create asset record
    connection.execute(
        """INSERT INTO document_assets
           (document_id, page_number, kind, file_path, caption, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (document_id, None, "upload", str(asset_path), image_path.stem, now()),
    )
    asset_ids = [connection.execute("SELECT last_insert_rowid()").fetchone()[0]]

    # Body includes filename keywords + OCR text
    body = f"[Image: {image_path.stem}]\n{ocr_text}".strip()
    return body, asset_ids


def ocr_page_snapshot(snapshot_path: Path) -> str:
    """OCR a page snapshot (for scanned PDFs)."""
    return ocr_image(snapshot_path)


def add_scanned_text_to_body(body: str, snapshot_path: Path, page_num: int) -> str:
    """Add OCR text from a page snapshot to the body under the page marker."""
    ocr_text = ocr_page_snapshot(snapshot_path)
    if ocr_text:
        page_marker = f"[[page:{page_num}]]"
        if page_marker in body:
            # Insert OCR text after the page marker
            body = body.replace(
                page_marker,
                f"{page_marker}\n[Scanned text]\n{ocr_text}",
            )
    return body
