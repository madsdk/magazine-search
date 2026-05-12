from pathlib import Path

import fitz  # PyMuPDF


def make_pdf(path: Path, num_pages: int = 2, page_text: list[str] | None = None) -> Path:
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page(width=200, height=300)
        text = page_text[i] if page_text and i < len(page_text) else f"Page {i + 1}"
        page.insert_text((20, 50), text)
    doc.save(str(path))
    doc.close()
    return path
