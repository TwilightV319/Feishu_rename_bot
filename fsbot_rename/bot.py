#!/usr/bin/env python3
"""
Feishu (Lark) Rename Bot – WebSocket server entry point.

User interaction flow
---------------------
1. User sends a text message containing "重命名" to start a conversation.
   The bot replies: "对话已开始，请直接发送需要重命名的文件（支持 pdf / jpg / png 等）。"

2. User sends an image or file. The bot:
   a. downloads the attachment,
   b. calls ``extractor`` to automatically extract (item_name, doc_type, amount),
   c. if auto-extraction succeeds:
        - builds a new filename via rename_helper,
        - looks up the sender's real name from the Feishu contact directory,
        - ensures a sub-folder named after the user exists in the target folder,
        - uploads the renamed file to that sub-folder,
        - replies with the result.
      the conversation stays active so the user can send more files.
   d. if auto-extraction fails:
        - asks the user to manually reply with: 物品名称 文档类型 金额

3. User sends a text message containing "结束".
   The bot clears the session and replies: "对话已结束。"

4. Sessions time out after ``pending_timeout`` seconds of inactivity.
"""

import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from lark_oapi.ws import Client as WSClient
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.event.dispatcher_handler import EventDispatcherHandler

from cleanup import remove_local_file
from config import settings
from extractor import extract_info
from lark_client import LarkClientWrapper
from rename_helper import build_new_filename, rename_file, sanitize_filename

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
client = LarkClientWrapper()

# pending_sessions: open_id -> {
#     "timestamp": float,            # last activity time
#     "awaiting_params": bool,       # True when auto-extraction failed & waiting for manual input
#     "local_path": Optional[str],   # downloaded file path (set when awaiting_params)
#     "original_name": Optional[str],
# }
pending_sessions: dict[str, dict[str, Any]] = {}
_sessions_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_expired_sessions() -> None:
    """Remove sessions that have exceeded the timeout."""
    now = time.time()
    expired = [
        oid
        for oid, sess in pending_sessions.items()
        if now - sess["timestamp"] > settings.pending_timeout
    ]
    for oid in expired:
        del pending_sessions[oid]
        logger.info("Expired session for %s", oid)


def _get_session(open_id: str) -> Optional[dict]:
    """Get the active session for a user, or None if expired / absent."""
    sess = pending_sessions.get(open_id)
    if sess is None:
        return None
    if time.time() - sess["timestamp"] > settings.pending_timeout:
        del pending_sessions[open_id]
        return None
    return sess


def _reset_session(open_id: str) -> dict:
    """Create or reset a session to the active state."""
    sess = {
        "timestamp": time.time(),
        "awaiting_params": False,
        "local_path": None,
        "original_name": None,
    }
    pending_sessions[open_id] = sess
    return sess


def _extract_file_info(message_type: str, content: str) -> Optional[tuple[str, str, str]]:
    """
    Extract resource info from a message.
    Returns (resource_type, file_key, original_name) or None.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None

    if message_type == "image":
        image_key = data.get("image_key")
        if image_key:
            return ("image", image_key, f"{image_key}.png")

    if message_type == "file":
        file_key = data.get("file_key")
        file_name = data.get("file_name", file_key)
        if file_key:
            return ("file", file_key, file_name)

    return None


def _parse_three_params(text: str) -> Optional[tuple[str, str, str]]:
    """
    Parse free-form text into (item_name, doc_type, amount).
    Expected format: three space-separated tokens.
    """
    parts = text.strip().split(maxsplit=2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _validate_filename_with_deepseek(filename: str, original_name: str) -> bool:
    """
    Ask DeepSeek to judge whether the renamed filename looks reasonable.
    Returns True if valid or if DeepSeek is not configured.
    """
    if not settings.deepseek_api_key:
        return True

    try:
        import openai
        ds_client = openai.OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )

        prompt = (
            f"请判断以下文件名是否合理。\n\n"
            f"原始文件名：{original_name}\n"
            f"重命名后的文件名：{filename}\n\n"
            f"请从以下两方面判断：\n"
            f"1. 格式是否正确：应为「物品名称_文档类型_金额.扩展名」的格式；\n"
            f"2. 物品名称部分是否看起来像正常的物品/服务名称或商家名称"
            f"（而不是乱码、无意义字符、APP包名、ID、随机字符串等）。\n\n"
            f"请只返回 JSON 格式，不要添加任何解释或 markdown 标记：\n"
            f'{{"valid": true/false, "reason": "简短说明"}}'
        )

        response = ds_client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )

        content = response.choices[0].message.content
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        # Robust JSON parsing fallback
        data: dict = {}
        if content:
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                # Try to extract the first JSON-ish object
                match = re.search(r'\{[^}]*"valid"[^}]*\}', content)
                if match:
                    try:
                        data = json.loads(match.group(0))
                    except json.JSONDecodeError:
                        pass

        is_valid = bool(data.get("valid", True))
        if not is_valid:
            logger.warning(
                "Filename validation failed for %s: %s",
                filename,
                data.get("reason", "unknown"),
            )
        return is_valid
    except Exception as exc:
        logger.warning("Filename validation error: %s", exc)
        return True


# ---------------------------------------------------------------------------
# Core file processing
# ---------------------------------------------------------------------------

def _process_file(
    open_id: str,
    message_id: str,
    local_path: Path,
    original_name: str,
    item_name: str,
    doc_type: str,
    amount: str,
) -> bool:
    """
    Rename, archive and upload a file.
    Returns True on success.
    """
    try:
        # 1. Rename with validation loop (max 5 attempts)
        current_path = local_path
        current_item, current_doc, current_amount = item_name, doc_type, amount
        is_valid = False

        for attempt in range(1, 6):
            new_name = build_new_filename(
                item_name=current_item,
                doc_type=current_doc,
                amount=current_amount,
                original_path=str(current_path),
            )
            renamed_path_str = rename_file(
                original_path=str(current_path),
                new_name=new_name,
                output_dir=str(local_path.parent),
                dry_run=False,
            )
            renamed_path = Path(renamed_path_str)
            current_path = renamed_path
            logger.info("Renamed (attempt %d): %s", attempt, renamed_path)

            # Validate filename via DeepSeek
            is_valid = _validate_filename_with_deepseek(renamed_path.name, original_name)
            if is_valid:
                break

            if attempt < 5:
                logger.warning(
                    "Filename validation failed (attempt %d/5), re-extracting: %s",
                    attempt,
                    renamed_path.name,
                )
                from extractor import reextract_image
                re_result = reextract_image(renamed_path, original_name, attempt=attempt + 1)
                if re_result is None:
                    break
                current_item, current_doc, current_amount = re_result
            else:
                logger.error(
                    "Filename validation failed after 5 attempts: %s",
                    renamed_path.name,
                )

        if not is_valid:
            client.reply_text(
                message_id,
                "文件自动识别失败（重试5次后仍无法确认内容），"
                "请检查文件清晰度后重试，或手动发送：物品名称 文档类型 金额",
            )
            return False

        # 2. Look up user name
        user_name = client.get_user_name(open_id, user_id_type="open_id")
        if not user_name:
            user_name = "未知用户"
        safe_user_name = sanitize_filename(user_name)

        # 3. Ensure sub-folder exists
        if not settings.folder_token:
            client.reply_text(
                message_id,
                "机器人尚未配置目标文件夹，请联系管理员设置 FOLDER_TOKEN。",
            )
            return False

        sub_folder_token = client.get_or_create_sub_folder(
            parent_token=settings.folder_token,
            folder_name=safe_user_name,
        )
        if sub_folder_token is None:
            client.reply_text(message_id, "创建归档文件夹失败，请稍后重试。")
            return False

        # 4. Upload to cloud docs
        uploaded_token = client.upload_file_to_folder(
            local_path=renamed_path,
            folder_token=sub_folder_token,
            file_name=renamed_path.name,
        )
        if uploaded_token is None:
            client.reply_text(message_id, "文件上传失败，请稍后重试。")
            return False

        # 5. Clean up local file after successful upload
        remove_local_file(str(renamed_path))

        # 6. Reply success
        client.reply_text(
            message_id,
            f"✅ 处理完成！\n"
            f"文件名：{renamed_path.name}\n"
            f"归档用户：{user_name}\n"
            f"已上传至云文档对应文件夹。\n\n"
            f"您还可以继续发送文件，或发送「结束」退出对话。",
        )
        logger.info(
            "Success for %s (%s) -> %s",
            open_id,
            user_name,
            renamed_path.name,
        )
        return True

    except Exception as exc:
        logger.exception("Error processing file: %s", exc)
        client.reply_text(message_id, f"处理时发生错误：{exc}")
        return False


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def _handle_text(open_id: str, message_id: str, text: str) -> None:
    """Process incoming text messages."""
    text_stripped = text.strip()
    lower = text_stripped.lower()

    # "结束" → close session
    if "结束" in text_stripped or "end" in lower:
        with _sessions_lock:
            if open_id in pending_sessions:
                del pending_sessions[open_id]
        client.reply_text(message_id, "对话已结束。如需再次使用，请发送「重命名」。")
        return

    # "重命名" → start / reset session
    if settings.command_prefix in text_stripped:
        with _sessions_lock:
            _reset_session(open_id)
        client.reply_text(
            message_id,
            "对话已开始 ✅\n"
            "请直接发送需要重命名的文件（支持 pdf / jpg / png 等）。\n\n"
            "机器人会自动识别文件内容并归档。",
        )
        return

    # If awaiting manual params
    with _sessions_lock:
        sess = _get_session(open_id)
        if sess and sess.get("awaiting_params"):
            parsed = _parse_three_params(text_stripped)
            if parsed is None:
                client.reply_text(
                    message_id,
                    "格式不正确，请直接回复三个参数（用空格分隔）：\n"
                    "物品名称 文档类型 金额\n"
                    "例如：办公用品 发票 128.50",
                )
                return

            item_name, doc_type, amount = parsed
            local_path = Path(sess["local_path"]) if sess["local_path"] else None
            original_name = sess.get("original_name", "")

            if local_path is None or not local_path.exists():
                client.reply_text(message_id, "文件已过期，请重新发送。")
                sess["awaiting_params"] = False
                sess["local_path"] = None
                return

            # Process and clear awaiting flag
            success = _process_file(
                open_id=open_id,
                message_id=message_id,
                local_path=local_path,
                original_name=original_name,
                item_name=item_name,
                doc_type=doc_type,
                amount=amount,
            )
            if success:
                sess["awaiting_params"] = False
                sess["local_path"] = None
                sess["original_name"] = None
                sess["timestamp"] = time.time()
            return

    # Unknown text while no active session
    client.reply_text(
        message_id,
        "如需使用重命名功能，请发送「重命名」开始对话。",
    )


def _handle_file_message(
    open_id: str, message_id: str, message_type: str, content: str
) -> None:
    """Process an image or file message from a user."""
    with _sessions_lock:
        _clean_expired_sessions()
        sess = _get_session(open_id)
        if sess is None:
            client.reply_text(
                message_id,
                "请先发送「重命名」开始对话，然后再发送文件。",
            )
            return

        # Reset awaiting flag (user sent a new file)
        sess["awaiting_params"] = False
        sess["local_path"] = None
        sess["original_name"] = None
        sess["timestamp"] = time.time()

    # Extract resource info
    res_info = _extract_file_info(message_type, content)
    if res_info is None:
        client.reply_text(message_id, "无法识别该附件，请重新发送。")
        return

    resource_type, file_key, original_name = res_info
    logger.info(
        "Received file from %s: type=%s key=%s name=%s",
        open_id,
        resource_type,
        file_key,
        original_name,
    )

    # 1. Download file
    local_path = client.download_message_resource(
        message_id=message_id,
        file_key=file_key,
        resource_type=resource_type,
        output_dir=settings.output_dir,
    )
    if local_path is None:
        client.reply_text(message_id, "文件下载失败，请稍后重试。")
        return

    # 2. Auto-extract info
    extracted = extract_info(local_path, original_name)

    if extracted is not None:
        item_name, doc_type, amount = extracted
        logger.info(
            "Auto-extracted: item=%s type=%s amount=%s",
            item_name,
            doc_type,
            amount,
        )

        # Inform user of extracted info before uploading
        client.reply_text(
            message_id,
            f"📎 收到文件，自动识别结果：\n"
            f"物品：{item_name}\n"
            f"类型：{doc_type}\n"
            f"金额：{amount}\n\n"
            f"正在归档并上传…",
        )

        success = _process_file(
            open_id=open_id,
            message_id=message_id,
            local_path=local_path,
            original_name=original_name,
            item_name=item_name,
            doc_type=doc_type,
            amount=amount,
        )
        if success:
            with _sessions_lock:
                s = _get_session(open_id)
                if s:
                    s["timestamp"] = time.time()
        return

    # 3. Auto-extraction failed → ask for manual input
    logger.warning("Auto-extraction failed for %s, asking user for params", open_id)
    with _sessions_lock:
        s = _get_session(open_id)
        if s:
            s["awaiting_params"] = True
            s["local_path"] = str(local_path)
            s["original_name"] = original_name
            s["timestamp"] = time.time()

    client.reply_text(
        message_id,
        "⚠️ 自动识别失败，请手动输入重命名参数（用空格分隔）：\n"
        "物品名称 文档类型 金额\n"
        "例如：办公用品 发票 128.50",
    )


# ---------------------------------------------------------------------------
# Main event router
# ---------------------------------------------------------------------------

def _on_im_message_receive_v1(event: P2ImMessageReceiveV1) -> None:
    """Entry point for every IM message received via WebSocket."""
    if not event.event:
        return

    msg = event.event.message
    sender = event.event.sender
    if not msg or not sender or not sender.sender_id:
        return

    open_id = sender.sender_id.open_id
    if not open_id:
        return

    message_type = msg.message_type or ""
    content = msg.content or ""
    message_id = msg.message_id or ""

    logger.debug("Received message from %s: type=%s", open_id, message_type)

    # Text messages
    if message_type == "text":
        try:
            data = json.loads(content)
            text = data.get("text", "")
        except json.JSONDecodeError:
            return
        _handle_text(open_id, message_id, text)
        return

    # Image / File messages
    if message_type in ("image", "file"):
        _handle_file_message(open_id, message_id, message_type, content)
        return


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def main() -> None:
    if not settings.folder_token:
        logger.warning(
            "FOLDER_TOKEN is not configured. "
            "Please add it to your .env file before uploading files."
        )

    handler = (
        EventDispatcherHandler.builder(
            encrypt_key=settings.encrypt_key,
            verification_token=settings.verification_token,
        )
        .register_p2_im_message_receive_v1(_on_im_message_receive_v1)
        .build()
    )

    ws = WSClient(
        app_id=settings.app_id,
        app_secret=settings.app_secret,
        event_handler=handler,
        auto_reconnect=True,
    )

    logger.info("Starting Feishu Rename Bot (WebSocket) …")
    try:
        ws.start()
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
