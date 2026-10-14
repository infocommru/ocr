import asyncio
import io
import os
import base64
import mimetypes
import time
import json  # Добавлено для парсинга JSON/JSONL
import aiohttp
from aiolimiter import AsyncLimiter
from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Yandex OCR Web Service")

# Получаем абсолютный путь к папке, где лежит этот файл (main.py)
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Монтируем статику по абсолютному пути
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Переменные окружения
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID")

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
POLL_TIMEOUT_SECONDS = 180         # Таймаут на polling операции OCR

# Лимитеры запросов к Yandex Cloud API
async_limiter_post = AsyncLimiter(max_rate=10, time_period=1)
async_limiter_get = AsyncLimiter(max_rate=50, time_period=1)


class OCRResult:
    def __init__(self, filename: str, mime_type: str):
        self.filename = filename
        self.mime_type = mime_type
        self.operation_id = None
        self.txt_filename = f"{os.path.splitext(filename)[0]}.txt"
        self.text = None
        self.error = None
        self.status = "pending"


def get_mime_type(file: UploadFile) -> str:
    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    content_type = (file.content_type or "").lower()
    
    if ext in [".jpg", ".jpeg"] or content_type in ["image/jpeg", "image/jpg"]:
        return "JPEG"
    elif ext == ".pdf" or content_type == "application/pdf":
        return "application/pdf"
    else:
        raise HTTPException(
            status_code=400,
            detail="Разрешены только файлы формата JPEG (.jpg, .jpeg) или PDF (.pdf)"
        )


async def start_processing(session: aiohttp.ClientSession, ocr_item: OCRResult, content_b64: str, headers: dict, model: str = "page"):
    async with async_limiter_post:
        ocr_item.status = "processing"
        data = {
            "mimeType": ocr_item.mime_type,
            "languageCodes": ["ru", "en"],
            "model": model,
            "content": content_b64
        }
        url = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeTextAsync"

        max_retries = 5
        for attempt in range(max_retries):
            try:
                async with session.post(url, headers=headers, json=data, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status == 200:
                        res_data = await response.json()
                        error = res_data.get("error")

                        if error and error.get("code") != 0:
                            ocr_item.error = f"Ошибка Yandex API ({error.get('code')}): {error.get('message', 'Неизвестно')}"
                            ocr_item.status = "failed"
                            return ocr_item

                        ocr_item.operation_id = res_data.get("id")
                        return ocr_item
                    else:
                        resp_text = await response.text()
                        if response.status in (429, 503) and attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        
                        ocr_item.error = f"HTTP {response.status}: {resp_text[:100]}"
                        ocr_item.status = "failed"
                        return ocr_item
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                ocr_item.error = f"Сетевая ошибка: {str(e)}"
                ocr_item.status = "failed"
                return ocr_item


async def get_result(session: aiohttp.ClientSession, ocr_item: OCRResult, headers: dict):
    if ocr_item.status == "failed" or not ocr_item.operation_id:
        return ocr_item

    url = f"https://ocr.api.cloud.yandex.net/ocr/v1/getRecognition?operationId={ocr_item.operation_id}"
    ready_url = f"https://operation.api.cloud.yandex.net/operations/{ocr_item.operation_id}"

    start_time = time.time()

    while True:
        if time.time() - start_time > POLL_TIMEOUT_SECONDS:
            ocr_item.error = f"Превышено время ожидания ответа ({POLL_TIMEOUT_SECONDS} сек)"
            ocr_item.status = "failed"
            return ocr_item

        try:
            async with async_limiter_get:
                async with session.get(ready_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as is_ready:
                    if is_ready.status == 200:
                        id_val = await is_ready.json()
                        error = id_val.get("error")

                        if error and error.get("code") != 0:
                            ocr_item.error = f"Ошибка операции: {error.get('message', 'Сбой распознавания')}"
                            ocr_item.status = "failed"
                            return ocr_item

                        if not id_val.get("done"):
                            await asyncio.sleep(1.5)
                            continue
                    else:
                        await asyncio.sleep(1.5)
                        continue

                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        response_text = await response.text()
                        
                        # PDF возвращает результат в формате JSONL (по строке на страницу)
                        if ocr_item.mime_type == "application/pdf":
                            texts = []
                            for line in response_text.strip().split('\n'):
                                if line.strip():
                                    try:
                                        page_data = json.loads(line)
                                        if "error" in page_data:
                                            ocr_item.error = f"Ошибка операции: {page_data['error'].get('message', 'Сбой распознавания')}"
                                            ocr_item.status = "failed"
                                            return ocr_item
                                        full_text = page_data.get("result", {}).get("textAnnotation", {}).get("fullText", "")
                                        if full_text:
                                            texts.append(full_text)
                                    except json.JSONDecodeError:
                                        continue
                            ocr_item.text = "\n\n--- Страница ---\n\n".join(texts)
                        else:
                            res_json = json.loads(response_text)
                            ocr_item.text = res_json.get("result", {}).get("textAnnotation", {}).get("fullText", "")
                        
                        ocr_item.status = "completed"
                        return ocr_item
                    else:
                        ocr_item.error = f"Ошибка загрузки текста HTTP {response.status}"
                        ocr_item.status = "failed"
                        return ocr_item

        except Exception as e:
            await asyncio.sleep(1.5)

@app.get("/", response_class=FileResponse)
async def get_index():
    return FileResponse("static/index.html")

@app.post("/scan-file")
async def run_scan_file(
    file: UploadFile = File(...),
    model: str = Form("page")  # Добавляем параметр с дефолтным значением
):
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        raise HTTPException(
            status_code=422,
            detail="Переменные окружения YANDEX_API_KEY или YANDEX_FOLDER_ID не заданы на сервере"
        )

    mime_type = get_mime_type(file)
    item = OCRResult(filename=file.filename or "file", mime_type=mime_type)

    try:
        content = await file.read(MAX_FILE_SIZE + 1)
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"Файл превышает лимит {MAX_FILE_SIZE // (1024 * 1024)} МБ"
            )
        if not content:
            raise HTTPException(status_code=400, detail="Пустой файл (0 байт)")

        if item.mime_type == "JPEG" and not content.startswith(b'\xff\xd8\xff'):
            raise HTTPException(
                status_code=400,
                detail="Файл не является валидным изображением JPEG"
            )
        elif item.mime_type == "application/pdf" and not content.startswith(b'%PDF'):
            raise HTTPException(
                status_code=400,
                detail="Файл не является валидным PDF-документом"
            )

        b64_content = base64.b64encode(content).decode("utf-8")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка чтения файла: {str(e)}")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "x-folder-id": YANDEX_FOLDER_ID,
        "x-data-logging-enabled": "true",
    }

    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as session:
        await start_processing(session, item, b64_content, headers, model)
        if item.status != "failed" and item.operation_id:
            await get_result(session, item, headers)

    if item.status == "completed" and item.text is not None:
        return JSONResponse(content={
            "filename": item.filename,
            "txt_filename": item.txt_filename,
            "text": item.text
        })
    else:
        raise HTTPException(
            status_code=422,
            detail=item.error or "Неизвестная ошибка распознавания Yandex API"
        )