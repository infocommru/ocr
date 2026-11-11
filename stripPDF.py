import pymupdf  # PyMuPDF
from fastapi import HTTPException

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

def strip_pdf(pdf_data: bytes) -> bytes :
    try:
        doc = pymupdf.open(stream=pdf_data, filetype="pdf")

        for page in doc:
            strip_page_text(page)

        return doc.tobytes()
    finally:
        doc.close()
        pymupdf.TOOLS.store_shrink(100)

def strip_page_text(page):
    blocks = page.get_text("dict",
        flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES,)["blocks"]

    total_spans = 0
    redactions = 0

    for block in blocks:
        if block.get("type") == 0:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    total_spans += 1

                    if not span.get("text", "").strip():
                        continue

                    rect = pymupdf.Rect(span["bbox"])
                    page.add_redact_annot(rect, fill=False)
                    redactions += 1

    if redactions:
        page.apply_redactions(
            images=0,
            graphics=0,
            text=0
        )

    return redactions

def split_pdf_to_images(pdf_content: bytes) -> list[tuple[int, bytes]]:
    """
    Разбивает PDF на список кортежей (имя_страницы, jpeg_bytes).
    Автоматически подбирает DPI, чтобы размер изображения был < MAX_IMAGE_SIZE.
    """
    try:
        doc = pymupdf.open(stream=pdf_content, filetype="pdf")
        images = []
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            
            # Начинаем с высокого DPI и понижаем, если файл слишком большой
            # Или начинаем с низкого и повышаем. Для OCR лучше начать с 300 и понижать.
            quality = 80
            img_bytes = None
            
            while quality >= 30:
                pix = page.get_pixmap()
                img_bytes = pix.tobytes("jpeg", jpg_quality=quality)
                
                if len(img_bytes) <= MAX_FILE_SIZE:
                    break
                
                quality -= 20

            if not img_bytes:
                raise HTTPException(status_code=400, detail=f"Невозможно конвертировать PDF в ОЗП")

            images.append(img_bytes)
        
        return images
    finally:
        doc.close()
        pymupdf.TOOLS.store_shrink(100)