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

async function processSingleFile(file) {
    const formData = new FormData();
    formData.append('file', file);

    formData.append('model', document.getElementById('modelSelect').value);

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
    const concurrencyLimit = 5;

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
                for (let i = pool.length - 1; i >= 0; i--) {
                    if (pool[i].isFulfilled) pool.splice(i, 1);
                }
            }
        }
        await Promise.all(pool);
    };

    await executePool();

    const successful = results.filter(r => r.success);
    const failed = results.filter(r => !r.success);

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