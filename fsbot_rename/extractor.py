#!/usr/bin/env python3
"""
Smart info extractor for invoice / payment screenshots.

Strategy (NO-AI first):
  1. PDF   → pdfplumber text extraction + regex rules
  2. Image → filename heuristic + keyword matching (NO AI by default)
  3. Fallback → OpenAI Vision ONLY when (a) key is configured AND
                (b) all non-AI methods returned "未知物品"

All extracted amounts are normalized through rename_helper.parse_amount().
"""

import base64
import json
import logging
import re
from pathlib import Path
from typing import Optional, Tuple

import pdfplumber
import pytesseract
from PIL import Image

from config import settings
from rename_helper import parse_amount, sanitize_filename

logger = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}


def extract_info(file_path: Path, original_name: str) -> Optional[Tuple[str, str, str]]:
    """
    Extract (item_name, doc_type, amount) from a downloaded file.
    Returns None if extraction fails completely.
    """
    ext = file_path.suffix.lower()
    result: Optional[Tuple[str, str, str]] = None

    if ext == ".pdf":
        result = _extract_from_pdf(file_path)
    elif ext in IMAGE_EXTS:
        result = _extract_from_image(file_path, original_name)
    else:
        result = _extract_from_filename(original_name)

    # Normalize amount through rename_helper
    if result is not None:
        item_name, doc_type, amount_raw = result
        return item_name, doc_type, parse_amount(amount_raw)

    return None


# ---------------------------------------------------------------------------
# PDF extraction (rule-based)
# ---------------------------------------------------------------------------

def _extract_from_pdf(path: Path) -> Optional[Tuple[str, str, str]]:
    try:
        text = ""
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"

        if not text.strip():
            return None

        amount = _extract_amount_from_text(text)
        doc_type = _extract_doc_type_from_text(text)
        item_name = _extract_item_name_from_text(text)
        return item_name, doc_type, amount
    except Exception:
        return None


def _extract_amount_from_text(text: str) -> str:
    """Try to find the total amount in Chinese invoice / receipt text."""
    priority_patterns = [
        r'价税合计[（(]大写[)）][^¥￥\n]*[¥￥]\s*([\d,]+\.?\d{0,2})',
        r'价税合计[^¥￥\n]*[¥￥]\s*([\d,]+\.?\d{0,2})',
        r'合计[（(]小写[)）][^¥￥\n]*[¥￥]\s*([\d,]+\.?\d{0,2})',
        r'合计金额[：:]\s*[¥￥]\s*([\d,]+\.?\d{0,2})',
        r'总金额[：:]\s*[¥￥]\s*([\d,]+\.?\d{0,2})',
        r'实付金额[：:]\s*[¥￥]\s*([\d,]+\.?\d{0,2})',
        r'支付金额[：:]\s*[¥￥]\s*([\d,]+\.?\d{0,2})',
    ]

    candidates = []
    for pat in priority_patterns:
        for match in re.finditer(pat, text):
            val = match.group(1).replace(",", "")
            try:
                f = float(val)
                if f > 0:
                    candidates.append((f, val, 10))
            except ValueError:
                continue

    # General ¥/￥ matches (lower priority)
    for match in re.finditer(r'[¥￥]\s*([\d,]+\.?\d{0,2})', text):
        val = match.group(1).replace(",", "")
        try:
            f = float(val)
            if f > 0:
                candidates.append((f, val, 1))
        except ValueError:
            continue

    if not candidates:
        return "0"

    candidates.sort(key=lambda x: (x[2], x[0]), reverse=True)
    return candidates[0][1]


def _extract_doc_type_from_text(text: str) -> str:
    scores = {"发票": 0, "收据": 0, "付款截图": 0}
    lower = text.lower()

    if "发票" in text or "增值税" in text or "invoice" in lower:
        scores["发票"] += 10
    if "收据" in text or "receipt" in lower or "收条" in text:
        scores["收据"] += 10
    if any(k in text for k in ("付款", "支付", "转账", "交易成功", "扫码支付")):
        scores["付款截图"] += 10
    if "alipay" in lower or "wechat" in lower or "微信支付" in text or "支付宝" in text:
        scores["付款截图"] += 5

    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "发票"


def _extract_item_name_from_text(text: str) -> str:
    # 1. 优先提取带税收编码的服务项目（电子发票表格格式）
    #    e.g. *信息系统服务*技术服务 12 150 1800.00 6% 108.00
    #         费
    coded = _extract_coded_service(text)
    if coded:
        return coded

    # 2. 传统的"项目名称/服务名称"字段
    goods_patterns = [
        r'货物或应税劳务、服务名称[：:]\s*([^\n]{2,40})',
        r'项目名称[：:]\s*([^\n]{2,40})',
        r'商品名称[：:]\s*([^\n]{2,40})',
        r'服务名称[：:]\s*([^\n]{2,40})',
    ]
    for pat in goods_patterns:
        match = re.search(pat, text)
        if match:
            name = match.group(1).strip()
            if name and len(name) < 40:
                return name

    # 3. 回退到销售方/商家名称
    merchant_patterns = [
        (r'销\s*名称[：:]\s*([^\n]{2,40})', 1),
        (r'售\s*方.*?名称[：:]\s*([^\n]{2,40})', 1),
        (r'销售方[名称]*[：:]\s*([^\n]{2,40})', 1),
        (r'销售方.*?\n.*?名称[：:]\s*([^\n]{2,40})', 1),
        (r'商户名称[：:]\s*([^\n]{2,40})', 1),
        (r'商家[名称]*[：:]\s*([^\n]{2,40})', 1),
        (r'收款方[名称]*[：:]\s*([^\n]{2,40})', 1),
        (r'卖方[名称]*[：:]\s*([^\n]{2,40})', 1),
        (r'店铺名称[：:]\s*([^\n]{2,40})', 1),
    ]
    for pat, group in merchant_patterns:
        match = re.search(pat, text)
        if match:
            name = match.group(group).strip().replace(" ", "").replace("　", "")
            if name and len(name) < 40:
                return name

    return "未知物品"


def _extract_coded_service(text: str) -> Optional[str]:
    """
    Extract service item from electronic invoice table format.
    Handles lines like:
        *信息系统服务*技术服务 12 150 1800.00 6% 108.00
        费
    Returns '技术服务费' or None.
    """
    # Match the coded service line: *编码*名称 数量 单价 金额...
    match = re.search(r'\*[^*\n]+\*([^\n]{2,60})', text)
    if not match:
        return None

    raw = match.group(1).strip()
    # Remove trailing numbers/amounts (split at first space followed by digits)
    cleaned = re.split(r'\s+\d', raw)[0].strip()

    # Strip trailing quantity units (e.g. "小风车 个" -> "小风车")
    _units = {'个', '件', '只', '套', '张', '盒', '支', '台', '条', '瓶', '包', '本', '副', '对', '片', '根', '块', '卷', '册', '组', '打', '码', '寸', '斤', '两', '千克', '克', 'kg', 'g', 'ml', 'l', 'm', 'cm', 'mm', 'km'}
    cleaned_parts = cleaned.split()
    if cleaned_parts and cleaned_parts[-1] in _units:
        cleaned_parts = cleaned_parts[:-1]
        cleaned = ''.join(cleaned_parts)

    # Try to append a short suffix from the very next line (e.g. "费" split by PDF line break)
    line_match = re.search(r'\*[^*\n]+\*[^\n]+\n([^\n]{1,4})\n', text)
    if line_match:
        suffix = line_match.group(1).strip()
        # Filter out quantity units
        if suffix in _units:
            suffix = ''
        # Only append if it's a short pure-text suffix without digits/currency
        if len(suffix) <= 3 and suffix and not re.search(r'[\d￥¥$]', suffix):
            cleaned += suffix

    cleaned = cleaned.replace(" ", "").replace("　", "")
    return cleaned if cleaned and len(cleaned) < 40 else None


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------

def _ocr_image(path: Path) -> str:
    """Run OCR on an image and return extracted Chinese + English text."""
    try:
        image = Image.open(path)
        text = pytesseract.image_to_string(image, lang="chi_sim+eng")
        return text.strip()
    except Exception as exc:
        logger.warning("OCR failed for %s: %s", path, exc)
        return ""


def _extract_with_deepseek(ocr_text: str, original_name: str, attempt: int = 1) -> Optional[Tuple[str, str, str]]:
    """Call DeepSeek API to analyze OCR text and extract invoice info."""
    if not settings.deepseek_api_key:
        return None

    try:
        import openai
        client = openai.OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )

        attempt_hint = ""
        if attempt > 1:
            attempt_hint = (
                f"（注意：这是第 {attempt} 次分析，前几次结果经校验不符合要求。"
                f"请务必仔细核对，确保 item_name 是正常的物品/服务名称或商家名称，"
                f"避免出现乱码、APP包名、无意义字符串等）\n\n"
            )

        prompt = (
            "以下是从一张发票或付款截图中通过OCR提取出的文字内容，以及原始文件名。"
            "请分析并提取以下三个信息：\n\n"
            f"{attempt_hint}"
            f"OCR文字内容：\n{ocr_text}\n\n"
            f"原始文件名：{original_name}\n\n"
            "请提取：\n"
            '1. item_name: 物品/服务名称或商家名称。如果无法识别请填"未知物品"\n'
            '2. doc_type: 文档类型，只能是以下之一：发票、收据、付款截图。如果无法识别请填"付款截图"\n'
            '3. amount: 金额数字，只保留数字和小数点，不要货币符号。如果无法识别请填0\n\n'
            '请以严格JSON格式返回，不要添加任何解释或markdown标记：\n'
            '{"item_name": "...", "doc_type": "...", "amount": "..."}'
        )

        response = client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.3,
            extra_body={"reasoning_effort": "low"},
        )

        content = response.choices[0].message.content
        content = _clean_json_response(content)

        data: dict = {}
        if content:
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                match = re.search(r'\{[^}]*"item_name"[^}]*\}', content)
                if match:
                    try:
                        data = json.loads(match.group(0))
                    except json.JSONDecodeError:
                        pass

        item_name = data.get("item_name") or "未知物品"
        doc_type = data.get("doc_type") or "付款截图"
        amount = data.get("amount") or "0"

        valid_types = {"发票", "收据", "付款截图"}
        if doc_type not in valid_types:
            doc_type = "付款截图"

        amount = re.sub(r"[^\d.]", "", str(amount))
        if not amount or amount == ".":
            amount = "0"

        return item_name, doc_type, amount
    except Exception as exc:
        logger.warning("DeepSeek extraction failed: %s", exc)
        return None


def reextract_image(path: Path, original_name: str, attempt: int = 1) -> Optional[Tuple[str, str, str]]:
    """Re-run OCR + DeepSeek for a given image (used after validation failure)."""
    ocr_text = _ocr_image(path)
    if not ocr_text:
        return None
    return _extract_with_deepseek(ocr_text, original_name, attempt)


def _extract_from_image(path: Path, original_name: str) -> Optional[Tuple[str, str, str]]:
    """
    Image extraction strategy:
      1. OCR + DeepSeek LLM (if key is configured)
      2. OpenAI Vision (if key is configured)
      3. Filename heuristic fallback
    """
    # 1. OCR + DeepSeek
    if settings.deepseek_api_key:
        ocr_text = _ocr_image(path)
        if ocr_text:
            result = _extract_with_deepseek(ocr_text, original_name)
            if result is not None:
                logger.info("DeepSeek extracted from image: %s", result)
                return result

    # 2. OpenAI Vision
    if settings.openai_api_key:
        result = _extract_with_openai(path)
        if result is not None:
            logger.info("OpenAI Vision extracted from image: %s", result)
            return result

    # 3. Filename heuristic
    return _extract_from_filename(original_name)


# ---------------------------------------------------------------------------
# Filename fallback
# ---------------------------------------------------------------------------

def _extract_from_filename(filename: str) -> Optional[Tuple[str, str, str]]:
    """
    Heuristic extraction from filename.
    Removes camera prefixes, then strips out doc-type keywords and amount numbers
    so they are not duplicated by build_new_filename().
    """
    name = Path(filename).stem
    # Remove common prefixes (camera, screenshot, app package markers)
    name = re.sub(
        r"^(IMG|img|Screenshot|screenshot|微信图片|QQ图片|微博图片|WX\d+|mmexport)\d*[-_]?(\d{8}[-_]?)?",
        "",
        name,
    )
    name = name.strip("-_. ")

    # Remove date/time patterns like 20260521_122753 or 2026-05-21_12-27-53
    name = re.sub(r'\d{8}[-_]?\d{6}|\d{4}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2}[-_]\d{2}', '', name)
    name = re.sub(r'\d{8}', '', name)  # standalone 8-digit date
    # Remove leftover 6-digit time (HHMMSS) that may remain after prefix removal
    name = re.sub(r'^\d{6}[-_]', '', name)
    name = name.strip("-_. ")
    lower = name.lower()

    # 1. Guess doc type and remove its keyword from the name
    # Longer keywords first to avoid partial matches (e.g. "付款" inside "付款截图")
    doc_type = "付款截图"
    doc_keywords = {
        "收据": ["receipt", "收据", "收条"],
        "付款截图": ["付款截图", "pay", "付款", "支付", "转账", "alipay", "wechat", "微信", "支付宝"],
        "发票": ["电子发票", "invoice", "发票", "vat"],
    }
    for dtype, keywords in doc_keywords.items():
        for kw in keywords:
            if kw in lower:
                doc_type = dtype
                # Use negative look-around to avoid partial matches like "pay" in "payment"
                name = re.sub(
                    rf'(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])',
                    "", name, flags=re.IGNORECASE
                )
                break
        if doc_type != "发票":
            break

    # 2. Extract amount and remove it from the name
    # Skip obvious time-like numbers (e.g. 122753 looks like HHMMSS)
    amount = "0"
    for match in re.finditer(r'(\d+\.\d{2})', name):
        val = match.group(1)
        # Skip if it looks like a time (e.g. 12.27 or 12.27.53)
        if float(val) < 100:
            continue
        amount = val
        break
    if amount == "0":
        for match in re.finditer(r'(\d+)', name):
            val = match.group(1)
            # Skip short numbers that look like time components (1227, 2026, etc.)
            if len(val) <= 4 and int(val) < 10000:
                continue
            amount = val
            break
    if amount != "0":
        name = name.replace(amount, "", 1)

    # 3. Clean up remaining name
    name = re.sub(r'[_\-\s]+', '_', name).strip("_")
    item_name = sanitize_filename(name) if name else "未知物品"
    if len(item_name) > 20:
        item_name = item_name[:20]

    return item_name, doc_type, amount


# ---------------------------------------------------------------------------
# OpenAI Vision (LAST RESORT ONLY)
# ---------------------------------------------------------------------------

def _extract_with_openai(path: Path) -> Optional[Tuple[str, str, str]]:
    try:
        from openai import OpenAI
    except ImportError:
        return None

    client = OpenAI(api_key=settings.openai_api_key)

    with open(path, "rb") as f:
        base64_image = base64.b64encode(f.read()).decode("utf-8")

    ext = path.suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    mime = mime_map.get(ext, "image/jpeg")

    prompt = (
        "请分析这张图片。它通常是一张发票、收据或付款截图。"
        "提取以下三个信息，并以严格JSON格式返回，不要添加任何解释或markdown标记：\n"
        "{\n"
        '  "item_name": "物品/服务名称或商家名称。如果无法识别请填未知物品",\n'
        '  "doc_type": "文档类型，只能是以下之一：发票、收据、付款截图。如果无法识别请填付款截图",\n'
        '  "amount": "金额数字，只保留数字和小数点，不要货币符号。如果无法识别请填0"\n'
        "}"
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{base64_image}",
                            },
                        },
                    ],
                }
            ],
            max_tokens=300,
        )

        content = response.choices[0].message.content
        content = _clean_json_response(content)
        data = json.loads(content)

        item_name = data.get("item_name") or "未知物品"
        doc_type = data.get("doc_type") or "付款截图"
        amount = data.get("amount") or "0"

        valid_types = {"发票", "收据", "付款截图"}
        if doc_type not in valid_types:
            doc_type = "付款截图"

        amount = re.sub(r"[^\d.]", "", str(amount))
        if not amount or amount == ".":
            amount = "0"

        return item_name, doc_type, amount
    except Exception:
        return None


def _clean_json_response(content: str) -> str:
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    elif content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    return content.strip()
