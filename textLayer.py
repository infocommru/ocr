#!/usr/bin/env python3
"""
add_text_layer.py

Добавляет невидимый текстовый слой в PDF по данным OCR.

На каждую страницу PDF — свой JSON; порядок JSON в списке соответствует
порядку страниц PDF (json_data[i] → страница i + 1).

Зависимости:
    pip install PyMuPDF
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pymupdf  # PyMuPDF

FONT_ALIAS = "OCRFONT"
BASELINE_RATIO = 0.8
DEFAULT_FIT_RATIO = 0.90
MIN_TEXT_CHARS = 20
BORN_DIGITAL_MIN_IMG_COVER = 0.30   # < 30% площади под картинкой → born-digital


# ---------------------------------------------------------------------------
# Загрузка JSON
# ---------------------------------------------------------------------------

def load_json_objects(text: str) -> list:
    """Разбирает один JSON. Если объектов несколько (склейка) — вернёт список."""
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    objs, idx, n = [], 0, len(text)
    while idx < n:
        while idx < n and text[idx] not in "{[":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
            objs.append(obj)
            idx = end
        except json.JSONDecodeError:
            idx += 1
    if not objs:
        raise ValueError("Не удалось разобрать JSON")
    return objs


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def _bbox_to_rect(b) -> Optional[tuple[float, float, float, float]]:
    if b is None:
        return None
    if isinstance(b, dict) and ("vertices" in b or "Vertices" in b):
        vs = b.get("vertices") or b.get("Vertices")
        xs = [float(v.get("x", 0) or 0) for v in vs]
        ys = [float(v.get("y", 0) or 0) for v in vs]
        if not xs or not ys:
            return None
        return (min(xs), min(ys), max(xs), max(ys))
    if isinstance(b, (list, tuple)) and len(b) == 4:
        return tuple(float(v) for v in b)  # type: ignore
    return None


def _parse_page(value, fallback: int) -> int:
    if value is None:
        return fallback
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            return int(str(value).strip())
        except Exception:
            return fallback


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (ValueError, TypeError):
        return None
    return f if f > 0 else None


# ---------------------------------------------------------------------------
# Парсеры (логика не изменена)
# ---------------------------------------------------------------------------

def _walk_yandex_ta(ta, page_no: int, out: list) -> Optional[tuple[float, float]]:
    for block in ta.get("blocks", []):
        for line in block.get("lines", []):
            for w in line.get("words", []):
                rect = _bbox_to_rect(w.get("boundingBox"))
                text = (w.get("text") or "").strip()
                if rect and text:
                    out.append((page_no, text, rect))
    w, h = _safe_float(ta.get("width")), _safe_float(ta.get("height"))
    return (w, h) if (w and h) else None


def _extract_yandex_result_wrapped(data, default_page: int = 1):
    if not (isinstance(data, dict) and "result" in data):
        return None
    inner = data["result"]
    if not isinstance(inner, dict):
        return None
    ta = inner.get("textAnnotation")
    if not ta:
        return None
    try:
        page_no = int(inner.get("page")) + 1 if inner.get("page") is not None else default_page
    except (ValueError, TypeError):
        page_no = default_page
    words: list = []
    size = _walk_yandex_ta(ta, page_no, words)
    sizes = {page_no: size} if size else {}
    return (words, sizes) if words else None


def _extract_yandex_vision(data, default_page: int = 1):
    if not (isinstance(data, dict) and "results" in data):
        return None
    words, sizes = [], {}
    try:
        outer = data["results"]
        if not isinstance(outer, list):
            return None

        def collect(ta, page_no):
            s = _walk_yandex_ta(ta, page_no, words)
            if s:
                sizes[page_no] = s

        for outer_idx, item in enumerate(outer):
            if not isinstance(item, dict):
                continue
            if "textAnnotation" in item:
                page_no = _parse_page(
                    item.get("page"),
                    default_page if len(outer) == 1 else outer_idx + 1,
                )
                collect(item["textAnnotation"], page_no)
                continue
            inner = item.get("results")
            if isinstance(inner, list):
                for inner_idx, sub in enumerate(inner):
                    if not isinstance(sub, dict):
                        continue
                    ta = sub.get("textAnnotation")
                    if not ta:
                        continue
                    page_no = _parse_page(
                        sub.get("page"),
                        default_page if len(inner) == 1 else inner_idx + 1,
                    )
                    collect(ta, page_no)
    except Exception:
        return None
    return (words, sizes) if words else None


def _extract_generic(data, default_page: int = 1):
    words: list = []

    def push(page, text, bbox):
        rect = _bbox_to_rect(bbox)
        text = (text or "").strip()
        if rect and text:
            words.append((_parse_page(page, default_page), text, rect))

    def get_bbox(item):
        return item.get("bbox") or item.get("box") or item.get("boundingBox")

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                push(item.get("page"), item.get("text", ""), get_bbox(item))
    elif isinstance(data, dict):
        if "pages" in data:
            for p_idx, page_item in enumerate(data["pages"]):
                page = (page_item.get("page")
                        or page_item.get("page_number")
                        or (p_idx + 1))
                for w in (page_item.get("words")
                          or page_item.get("items")
                          or page_item.get("blocks") or []):
                    push(page, w.get("text", ""), get_bbox(w))
        elif "words" in data:
            for w in data["words"]:
                push(w.get("page"), w.get("text", ""), get_bbox(w))
        elif "text" in data and ("bbox" in data or "box" in data or "boundingBox" in data):
            push(data.get("page"), data["text"], get_bbox(data))
    return (words, {}) if words else None


# ---------------------------------------------------------------------------
# Извлечение слов: 1 JSON == 1 страница
# ---------------------------------------------------------------------------

def extract_words_multi(json_data: list[str]) -> tuple[list, dict]:
    """
    Возвращает (words, sizes), где words — [(page_no, text, bbox), ...].
    Номер страницы = индекс JSON в списке + 1 (независимо от поля page внутри JSON).
    """
    all_words: list = []
    all_sizes: dict[int, tuple[float, float]] = {}

    for idx, text in enumerate(json_data):
        page_no = idx + 1
        result = None

        for obj in load_json_objects(text):
            for parser in (_extract_yandex_result_wrapped,
                           _extract_yandex_vision,
                           _extract_generic):
                result = parser(obj, default_page=page_no)
                if result:
                    break
            if result:
                break

        if not result:
            continue

        words, sizes = result
        # Принудительно закрепляем все слова за страницей этого JSON
        all_words.extend((page_no, t, b) for _, t, b in words)
        for s in sizes.values():
            all_sizes[page_no] = s

    if not all_words:
        raise ValueError("Не удалось распознать формат JSON (нет слов с координатами)")
    return all_words, all_sizes


# ---------------------------------------------------------------------------
# Шрифты / масштаб
# ---------------------------------------------------------------------------

def find_font(explicit: Optional[str] = None) -> Optional[str]:
    if explicit:
        return explicit if Path(explicit).is_file() else None
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ]:
        if Path(p).is_file():
            return p
    return None


def page_scales(page, annot_size, fallback_dpi: float) -> tuple[float, float]:
    fb = 72.0 / float(fallback_dpi)

    if not annot_size:
        return fb, fb
    
    aw, ah = annot_size

    if aw <= 0 or ah <= 0:
        return fb, fb
    
    sx = page.rect.width / aw
    sy = page.rect.height / ah

    if abs(sx - sy) > 0.05 * max(sx, sy):
        return fb, fb
    
    return sx, sy


def fit_fontsize(font_obj, text: str, max_w: float, start_fs: float,
                 fit_ratio: float = DEFAULT_FIT_RATIO) -> float:
    fs = start_fs
    for _ in range(80):
        if fs <= 0.5:
            break
        try:
            tw = font_obj.text_length(text, fontsize=fs)
        except Exception:
            tw = max_w
        if tw <= max_w * fit_ratio:
            break
        fs *= 0.93
    return max(fs, 0.5)


# ---------------------------------------------------------------------------
# Работа с существующим текстом
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Наложение текстового слоя
# ---------------------------------------------------------------------------

def add_text_layer(
    pdf_data: bytes,
    json_data: list[str],
    dpi: float = 72.0,
    font_path: Optional[str] = None,
    fit_ratio: float = DEFAULT_FIT_RATIO,
    space_per_word: bool = True,
    sort_spatial: bool = True,
    min_gap: float = 0.0,
) -> Optional[bytes]:
    """
    pdf_data   — содержимое PDF;
    json_data  — список JSON, по одному на страницу PDF (индекс = страница − 1).
    Возвращает байты PDF или None, если ни одного слова не добавлено.
    """
    words, page_sizes = extract_words_multi(json_data)

    by_page: dict[int, list] = {}
    for page, text, bbox in words:
        by_page.setdefault(page, []).append((text, bbox))

    font_file = find_font(font_path)
    try:
        font_obj = pymupdf.Font(fontfile=font_file) if font_file else pymupdf.Font("helv")
    except Exception:
        font_obj = pymupdf.Font("helv")
        font_file = None
    insert_fontname = FONT_ALIAS if font_file else "helv"

    doc = pymupdf.open(stream=pdf_data, filetype="pdf")
    inserted = 0

    try:
        for page_num in sorted(by_page.keys()):
            items = by_page[page_num]
            idx = page_num - 1
            if not (0 <= idx < doc.page_count):
                continue
            page = doc[idx]

            strip_page_text(page)

            sx, sy = page_scales(page, page_sizes.get(page_num), dpi)
            if font_file:
                page.insert_font(fontname=FONT_ALIAS, fontfile=font_file)

            if sort_spatial:
                items_sorted = sorted(
                    items,
                    key=lambda it: (round((it[1][1] + it[1][3]) / 2 / 5) * 5, it[1][0]),
                )
            else:
                items_sorted = items

            prev_x1: Optional[float] = None
            prev_yc: Optional[float] = None

            for text, (x0, y0, x1, y1) in items_sorted:
                X0, Y0 = x0 * sx, y0 * sy
                X1, Y1 = x1 * sx, y1 * sy
                W, H = X1 - X0, Y1 - Y0
                if W <= 0.5 or H <= 0.5:
                    continue

                fs = fit_fontsize(font_obj, text, W, H, fit_ratio=fit_ratio)

                if min_gap > 0 and prev_x1 is not None and prev_yc is not None:
                    yc = (Y0 + Y1) / 2
                    if abs(yc - prev_yc) < H * 0.5:
                        gap = X0 - prev_x1
                        if gap < min_gap:
                            X0 += (min_gap - gap)

                try:
                    rendered_w = font_obj.text_length(text, fontsize=fs)
                except Exception:
                    rendered_w = W * fit_ratio

                insert_text = (text + " ") if space_per_word else text
                baseline_y = Y0 + H * BASELINE_RATIO

                try:
                    page.insert_text(
                        (X0, baseline_y), insert_text,
                        fontname=insert_fontname, fontsize=fs,
                        color=(0, 0, 0), render_mode=3, overlay=True,
                    )
                    inserted += 1
                except Exception:
                    pass

                prev_x1 = X0 + rendered_w
                prev_yc = (Y0 + Y1) / 2

        if inserted == 0:
            return None

        return doc.tobytes()
    finally:
        doc.close()
        pymupdf.TOOLS.store_shrink(100)