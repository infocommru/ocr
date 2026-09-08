import asyncio
import io
import os
import base64
import mimetypes
import time
import aiohttp
from aiolimiter import AsyncLimiter
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Yandex OCR Web Service")

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
    mime, _ = mimetypes.guess_type(file.filename or "")
    if not mime:
        mime = file.content_type or "image/jpeg"
    
    mime_upper = mime.split("/")[-1].upper()
    if mime_upper == "JPG":
        mime_upper = "JPEG"
    return mime_upper


async def start_processing(session: aiohttp.ClientSession, ocr_item: OCRResult, content_b64: str, headers: dict):
    async with async_limiter_post:
        ocr_item.status = "processing"
        data = {
            "mimeType": ocr_item.mime_type,
            "languageCodes": ["ru", "en"],
            "model": "page",
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
                        res_json = await response.json()
                        text = res_json.get("result", {}).get("textAnnotation", {}).get("fullText", "")
                        ocr_item.text = text
                        ocr_item.status = "completed"
                        return ocr_item
                    else:
                        ocr_item.error = f"Ошибка загрузки текста HTTP {response.status}"
                        ocr_item.status = "failed"
                        return ocr_item

        except Exception as e:
            await asyncio.sleep(1.5)


HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Yandex OCR Service</title>
    <!-- Подключаем JSZip для создания архива прямо в браузере -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js"></script>
    <style>
        :root {
            --primary: #2563eb;
            --primary-hover: #1d4ed8;
            --bg: #f8fafc;
            --card: #ffffff;
            --text: #1e293b;
            --border: #e2e8f0;
            --error: #ef4444;
            --error-bg: #fef2f2;
            --success: #10b981;
            --success-bg: #ecfdf5;
            --warning: #f59e0b;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 40px 20px;
            display: flex;
            justify-content: center;
        }

        .container {
            width: 100%;
            max-width: 650px;
            background: var(--card);
            border-radius: 12px;
            padding: 32px;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.05), 0 8px 10px -6px rgba(0, 0, 0, 0.01);
            border: 1px solid var(--border);
        }

        h2 {
            margin-top: 0;
            font-size: 1.5rem;
            color: #0f172a;
        }

        p.subtitle {
            color: #64748b;
            font-size: 0.95rem;
            margin-top: -8px;
            margin-bottom: 24px;
        }

        .dropzone {
            border: 2px dashed var(--border);
            border-radius: 8px;
            padding: 30px 20px;
            text-align: center;
            background: #fafafa;
            cursor: pointer;
            transition: all 0.2s ease;
            position: relative;
        }

        .dropzone:hover {
            border-color: var(--primary);
            background: #f0f7ff;
        }

        .dropzone input[type="file"] {
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            opacity: 0;
            cursor: pointer;
        }

        .file-info {
            margin-top: 12px;
            font-size: 0.9rem;
            color: #475569;
            font-weight: 500;
        }

        button.btn-submit {
            width: 100%;
            margin-top: 20px;
            padding: 12px 20px;
            background-color: var(--primary);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: background-color 0.2s;
        }

        button.btn-submit:hover {
            background-color: var(--primary-hover);
        }

        button.btn-submit:disabled {
            background-color: #94a3b8;
            cursor: not-allowed;
        }

        .progress-wrapper {
            margin-top: 24px;
            display: none;
        }

        .progress-header {
            display: flex;
            justify-content: space-between;
            font-size: 0.875rem;
            font-weight: 600;
            margin-bottom: 8px;
        }

        .progress-bar-bg {
            width: 100%;
            height: 10px;
            background-color: #e2e8f0;
            border-radius: 999px;
            overflow: hidden;
        }

        .progress-bar-fill {
            height: 100%;
            width: 0%;
            background-color: var(--primary);
            border-radius: 999px;
            transition: width 0.3s ease;
        }

        .status-box {
            margin-top: 20px;
            padding: 16px;
            border-radius: 8px;
            font-size: 0.9rem;
            display: none;
        }

        .status-box.success {
            background-color: var(--success-bg);
            border: 1px solid #a7f3d0;
            color: #065f46;
        }

        .status-box.warning {
            background-color: #fffbeb;
            border: 1px solid #fde68a;
            color: #92400e;
        }

        .status-box.error {
            background-color: var(--error-bg);
            border: 1px solid #fecaca;
            color: #991b1b;
        }

        .error-list {
            margin-top: 12px;
            padding-left: 0;
            list-style: none;
        }

        .error-item {
            display: flex;
            flex-direction: column;
            gap: 4px;
            padding: 8px 12px;
            background: rgba(255, 255, 255, 0.8);
            border-radius: 6px;
            margin-bottom: 6px;
            font-size: 0.85rem;
            border: 1px solid rgba(0,0,0,0.08);
        }

        .error-filename {
            font-weight: 600;
            word-break: break-all;
            color: #1e293b;
        }

        .error-reason {
            color: var(--error);
        }
    </style>
</head>
<body>

<div class="container">
    <h2>🔍 Yandex OCR Scanner</h2>
    <p class="subtitle">Загрузите изображения или PDF для распознавания текста</p>

    <form id="ocrForm">
        <div class="dropzone" id="dropzone">
            <input type="file" id="fileInput" name="files" multiple accept="image/*,application/pdf">
            <div style="font-size: 2rem;">📁</div>
            <div style="margin-top: 8px;">Перетащите файлы сюда или нажмите для выбора</div>
            <div class="file-info" id="fileCountText">Файлы не выбраны</div>
        </div>

        <button type="submit" class="btn-submit" id="submitBtn" disabled>Запустить обработку</button>
    </form>

    <div class="progress-wrapper" id="progressWrapper">
        <div class="progress-header">
            <span id="progressStatusText">Подготовка...</span>
            <span id="progressPercentText">0%</span>
        </div>
        <div class="progress-bar-bg">
            <div class="progress-bar-fill" id="progressBarFill"></div>
        </div>
    </div>

    <div class="status-box" id="statusBox"></div>
</div>

<script>
    const fileInput = document.getElementById('fileInput');
    const fileCountText = document.getElementById('fileCountText');
    const submitBtn = document.getElementById('submitBtn');
    const ocrForm = document.getElementById('ocrForm');

    const progressWrapper = document.getElementById('progressWrapper');
    const progressBarFill = document.getElementById('progressBarFill');
    const progressStatusText = document.getElementById('progressStatusText');
    const progressPercentText = document.getElementById('progressPercentText');
    const statusBox = document.getElementById('statusBox');

    fileInput.addEventListener('change', () => {
        const count = fileInput.files.length;
        if (count > 0) {
            fileCountText.innerText = `Выбрано файлов: ${count}`;
            submitBtn.disabled = false;
        } else {
            fileCountText.innerText = 'Файлы не выбраны';
            submitBtn.disabled = true;
        }
    });

    function updateProgress(completed, total) {
        const percent = (completed / total) * 100;
        progressBarFill.style.width = `${percent}%`;
        progressPercentText.innerText = `${Math.round(percent)}%`;
        progressStatusText.innerText = `Обработано ${completed} из ${total} файлов`;
    }

    // Вспомогательная функция отправки одиночного файла
    async function processSingleFile(file) {
        const formData = new FormData();
        formData.append('file', file);

        try {
            const res = await fetch('/scan-file', {
                method: 'POST',
                body: formData
            });

            if (res.ok) {
                const data = await res.json();
                return { success: true, filename: file.name, txtFilename: data.txt_filename, text: data.text };
            } else {
                const errData = await res.json();
                const reason = typeof errData.detail === 'string' ? errData.detail : (errData.detail?.message || 'Сбой обработки');
                return { success: false, filename: file.name, error: reason };
            }
        } catch (err) {
            return { success: false, filename: file.name, error: 'Сетевая ошибка сервера' };
        }
    }

    ocrForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const files = Array.from(fileInput.files);
        const totalFiles = files.length;
        if (totalFiles === 0) return;

        submitBtn.disabled = true;
        statusBox.style.display = 'none';
        statusBox.className = 'status-box';
        progressWrapper.style.display = 'block';
        updateProgress(0, totalFiles);

        let completedCount = 0;
        const results = [];
        const concurrencyLimit = 5; // Одновременно отправляем не более 5 файлов

        // Пул выполнения файлов с честным счетчиком
        const executePool = async () => {
            const pool = [];
            for (const file of files) {
                const task = processSingleFile(file).then(res => {
                    completedCount++;
                    updateProgress(completedCount, totalFiles);
                    results.push(res);
                });
                pool.push(task);

                if (pool.length >= concurrencyLimit) {
                    await Promise.race(pool);
                    // Удаляем завершенные задачи из пула
                    for (let i = pool.length - 1; i >= 0; i--) {
                        if (pool[i].isFulfilled) pool.splice(i, 1);
                    }
                }
            }
            await Promise.all(pool);
        };

        await executePool();

        // Фильтрация результатов
        const successful = results.filter(r => r.success);
        const failed = results.filter(r => !r.success);

        // Если есть хоть один распознанный файл — запаковываем в ZIP через JSZip
        if (successful.length > 0) {
            const zip = new JSZip();
            const usedNames = {};

            successful.forEach(item => {
                let name = item.txtFilename;
                if (usedNames[name]) {
                    usedNames[name]++;
                    const parts = name.split('.');
                    const ext = parts.pop();
                    name = `${parts.join('.')}_${usedNames[name]}.${ext}`;
                } else {
                    usedNames[name] = 1;
                }
                zip.file(name, item.text);
            });

            const content = await zip.generateAsync({ type: 'blob' });
            const downloadUrl = window.URL.createObjectURL(content);
            const a = document.createElement('a');
            a.href = downloadUrl;
            a.download = 'ocr_results.zip';
            document.body.appendChild(a);
            a.click();
            a.remove();
        }

        // Отрисовка статуса
        statusBox.style.display = 'block';
        if (failed.length === 0) {
            statusBox.className = 'status-box success';
            statusBox.innerHTML = `<strong>Успешно!</strong> Все файлы (${successful.length}) обработаны. Архив <code>ocr_results.zip</code> скачан.`;
        } else if (successful.length > 0) {
            statusBox.className = 'status-box warning';
            let html = `<strong>Частично выполнено:</strong> Распознано ${successful.length} из ${totalFiles}. Ошибок: ${failed.length}.<ul class="error-list">`;
            failed.forEach(err => {
                html += `<li class="error-item"><span class="error-filename">📄 ${err.filename}</span><span class="error-reason">❌ ${err.error}</span></li>`;
            });
            html += '</ul>';
            statusBox.innerHTML = html;
        } else {
            statusBox.className = 'status-box error';
            let html = `<strong>Ошибка:</strong> Ни один файл не был распознан.<ul class="error-list">`;
            failed.forEach(err => {
                html += `<li class="error-item"><span class="error-filename">📄 ${err.filename}</span><span class="error-reason">❌ ${err.error}</span></li>`;
            });
            html += '</ul>';
            statusBox.innerHTML = html;
        }

        submitBtn.disabled = false;
    });
</script>

</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def get_index():
    return HTMLResponse(content=HTML_CONTENT)


@app.post("/scan-file")
async def run_scan_file(file: UploadFile = File(...)):
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
        await start_processing(session, item, b64_content, headers)
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