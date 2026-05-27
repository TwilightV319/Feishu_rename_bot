"""
Lark (Feishu) API client wrapper.
Handles file download, user lookup, folder creation and file upload.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from lark_oapi import Client
from lark_oapi.api.contact.v3.model.get_user_request import GetUserRequest
from lark_oapi.api.drive.v1.model.create_folder_file_request import (
    CreateFolderFileRequest,
)
from lark_oapi.api.drive.v1.model.create_folder_file_request_body import (
    CreateFolderFileRequestBody,
)
from lark_oapi.api.drive.v1.model.list_file_request import ListFileRequest
from lark_oapi.api.drive.v1.model.upload_all_file_request import UploadAllFileRequest
from lark_oapi.api.drive.v1.model.upload_all_file_request_body import (
    UploadAllFileRequestBody,
)
from lark_oapi.api.im.v1.model.create_message_request import CreateMessageRequest
from lark_oapi.api.im.v1.model.create_message_request_body import (
    CreateMessageRequestBody,
)
from lark_oapi.api.im.v1.model.get_message_resource_request import (
    GetMessageResourceRequest,
)
from lark_oapi.api.im.v1.model.reply_message_request import ReplyMessageRequest
from lark_oapi.api.im.v1.model.reply_message_request_body import (
    ReplyMessageRequestBody,
)

from config import settings

logger = logging.getLogger(__name__)


class LarkClientWrapper:
    """High-level wrapper around the official Lark SDK."""

    def __init__(self) -> None:
        self.client = (
            Client.builder()
            .app_id(settings.app_id)
            .app_secret(settings.app_secret)
            .log_level(getattr(__import__("lark_oapi").core.enum.LogLevel, settings.log_level.upper(), 20))
            .build()
        )
        # In-memory cache: user_open_id -> user_name
        self._user_name_cache: dict[str, str] = {}
        # In-memory cache: folder_token -> {sub_folder_name: sub_folder_token}
        self._folder_cache: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # User
    # ------------------------------------------------------------------
    def get_user_name(self, user_id: str, user_id_type: str = "open_id") -> Optional[str]:
        """Query contact directory and return the user's name."""
        cache_key = f"{user_id_type}:{user_id}"
        if cache_key in self._user_name_cache:
            return self._user_name_cache[cache_key]

        req = (
            GetUserRequest.builder()
            .user_id_type(user_id_type)
            .user_id(user_id)
            .build()
        )
        resp = self.client.contact.v3.user.get(req)
        if resp.code != 0:
            logger.error("Failed to get user info: %s", resp.msg)
            return None

        user = resp.data.user if resp.data else None
        if user is None:
            return None

        name = user.name or user.en_name or user.nickname or user_id
        if name == user_id:
            logger.warning(
                "获取用户姓名失败，已使用 user_id(%s) 作为文件夹名称。"
                "可能原因：1) 机器人缺少 contact:user.base:readonly 权限；"
                "2) 用户未设置姓名/英文名/昵称；"
                "3) 用户不在机器人可见范围内（外部用户/跨租户）。",
                user_id,
            )
        self._user_name_cache[cache_key] = name
        return name

    # ------------------------------------------------------------------
    # Message helpers
    # ------------------------------------------------------------------
    def reply_text(self, message_id: str, text: str) -> None:
        """Reply to a message with plain text."""
        content = json.dumps({"text": text}, ensure_ascii=False)
        body = (
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(content)
            .build()
        )
        req = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        resp = self.client.im.v1.message.reply(req)
        if resp.code != 0:
            logger.error("Failed to reply message: %s", resp.msg)

    def send_text(self, receive_id: str, text: str, receive_id_type: str = "open_id") -> None:
        """Send a text message to a user or chat."""
        content = json.dumps({"text": text}, ensure_ascii=False)
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(content)
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        resp = self.client.im.v1.message.create(req)
        if resp.code != 0:
            logger.error("Failed to send message: %s", resp.msg)

    # ------------------------------------------------------------------
    # File download
    # ------------------------------------------------------------------
    def download_message_resource(
        self, message_id: str, file_key: str, resource_type: str, output_dir: str
    ) -> Optional[Path]:
        """
        Download an image or file attached to a message.
        Returns the local file path.
        """
        req = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        resp = self.client.im.v1.message_resource.get(req)
        if resp.code != 0:
            logger.error("Failed to download resource: %s", resp.msg)
            return None

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        file_name = getattr(resp, "file_name", None) or file_key
        # Sanitize filename
        file_name = file_name.replace("/", "_").replace("\\", "_")
        local_path = out_dir / file_name

        try:
            with open(local_path, "wb") as f:
                # resp.file is a file-like object (bytesio or similar)
                f.write(resp.file.read())
            return local_path
        except Exception as exc:
            logger.error("Failed to save downloaded file: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Drive / Cloud docs
    # ------------------------------------------------------------------
    def get_or_create_sub_folder(self, parent_token: str, folder_name: str) -> Optional[str]:
        """
        Look for a sub-folder named *folder_name* inside *parent_token*.
        If it does not exist, create it.
        Returns the folder token.
        """
        # Check cache first
        cache = self._folder_cache.setdefault(parent_token, {})
        if folder_name in cache:
            return cache[folder_name]

        # List existing children
        page_token: Optional[str] = None
        while True:
            builder = ListFileRequest.builder().folder_token(parent_token).page_size(200)
            if page_token:
                builder = builder.page_token(page_token)
            req = builder.build()
            resp = self.client.drive.v1.file.list(req)
            if resp.code != 0:
                logger.error("Failed to list folder contents: %s", resp.msg)
                break

            files = resp.data.files if resp.data and resp.data.files else []
            for f in files:
                if f.type == "folder" and f.name == folder_name:
                    token = f.token
                    cache[folder_name] = token
                    return token

            if not (resp.data and resp.data.has_more and resp.data.next_page_token):
                break
            page_token = resp.data.next_page_token

        # Not found — create it
        body = (
            CreateFolderFileRequestBody.builder()
            .name(folder_name)
            .folder_token(parent_token)
            .build()
        )
        req = (
            CreateFolderFileRequest.builder()
            .request_body(body)
            .build()
        )
        resp = self.client.drive.v1.file.create_folder(req)
        if resp.code != 0:
            logger.error("Failed to create folder '%s': %s", folder_name, resp.msg)
            return None

        token = resp.data.token if resp.data else None
        if token:
            cache[folder_name] = token
        return token

    def upload_file_to_folder(
        self, local_path: Path, folder_token: str, file_name: Optional[str] = None
    ) -> Optional[str]:
        """
        Upload a local file to a cloud-docs folder.
        Returns the uploaded file token.
        """
        upload_name = file_name or local_path.name
        file_size = local_path.stat().st_size

        with open(local_path, "rb") as f:
            body = (
                UploadAllFileRequestBody.builder()
                .file_name(upload_name)
                .parent_type("explorer")
                .parent_node(folder_token)
                .size(file_size)
                .file(f)
                .build()
            )
            req = (
                UploadAllFileRequest.builder()
                .request_body(body)
                .build()
            )
            resp = self.client.drive.v1.file.upload_all(req)

        if resp.code != 0:
            logger.error("Failed to upload file '%s': %s", upload_name, resp.msg)
            return None

        return resp.data.file_token if resp.data else None
