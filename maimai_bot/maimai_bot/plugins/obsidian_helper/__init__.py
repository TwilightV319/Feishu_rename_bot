import asyncio
import json
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters import Bot, Event
from nonebot.params import EventPlainText
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from nonebot.adapters.onebot.v11 import (
    Bot as OneBotV11Bot,
    GroupMessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="obsidian_helper",
    description="",
    usage="ob question",
    config=Config,
)

cfg = get_plugin_config(Config)
CACHE_DIR = Path("data") / "obsidian_helper"
INDEX_FILE = CACHE_DIR / "help_index.json"
PLUGIN_INDEX_FILE = CACHE_DIR / "community_plugins.json"
INDEX_LOCK = asyncio.Lock()
PLUGIN_INDEX_LOCK = asyncio.Lock()
PLUGIN_STORE_URL = "https://obsidian.md/plugins?id={plugin_id}"


def extract_obsidian_question(text: str) -> str:
    match = re.match(r"(?is)^ob(?:\s+|$)(.*)$", text.strip())
    if not match:
        return ""
    return match.group(1).strip()


async def starts_with_ob(event: Event) -> bool:
    return bool(extract_obsidian_question(event.get_plaintext()))


obsidian_matcher = on_message(rule=Rule(starts_with_ob), priority=10, block=True)


class HelpHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.ignored_depth = 0
        self.current_tag = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.current_tag = tag
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag in {"h1", "h2", "h3", "h4"}:
            self.parts.append(f"\n{'#' * int(tag[1])} ")
        elif tag in {"p", "section", "article", "div", "ul", "ol", "li", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.ignored_depth:
            self.ignored_depth -= 1
            return
        if self.ignored_depth:
            return
        if tag in {"p", "section", "article", "div", "ul", "ol", "li"}:
            self.parts.append("\n")
        self.current_tag = ""

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self.current_tag == "title":
            self.title_parts.append(text)
        self.parts.append(text)
        self.parts.append(" ")

    def text(self) -> str:
        content = "".join(self.parts)
        content = re.sub(r"[ \t]+\n", "\n", content)
        content = re.sub(r"\n{3,}", "\n\n", content)
        return content.strip()

    def title(self) -> str:
        return " ".join(self.title_parts).strip()


def build_client_kwargs() -> dict[str, Any]:
    client_kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(30.0, connect=10.0),
        "follow_redirects": True,
    }
    if cfg.obsidian_proxy:
        client_kwargs["proxy"] = cfg.obsidian_proxy
    return client_kwargs


def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def is_cache_fresh(payload: dict[str, Any], ttl_hours: int) -> bool:
    updated_at = payload.get("updated_at", 0)
    ttl_seconds = max(ttl_hours, 1) * 3600
    return time.time() - updated_at < ttl_seconds


def url_to_doc(url: str, idx: int) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path).strip("/")
    if path.startswith("help/"):
        path = path[5:]
    slug = path or "home"
    title = " / ".join(
        segment.replace("-", " ").replace("_", " ").title()
        for segment in slug.split("/")
    )
    return {
        "id": idx,
        "title": title or "Home",
        "slug": slug,
        "url": url,
    }


def parse_sitemap(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    urls: list[str] = []
    for loc in root.findall(".//{*}loc"):
        if loc.text:
            urls.append(loc.text.strip())
    return urls


def load_cached_index() -> list[dict[str, Any]] | None:
    if not INDEX_FILE.exists():
        return None
    try:
        payload = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not payload.get("docs"):
        return None
    if not is_cache_fresh(payload, cfg.obsidian_help_cache_ttl_hours):
        return None
    return payload["docs"]


def load_any_cached_index() -> list[dict[str, Any]] | None:
    if not INDEX_FILE.exists():
        return None
    try:
        payload = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    docs = payload.get("docs")
    return docs if docs else None


def build_plugin_store_url(plugin_id: str) -> str:
    return PLUGIN_STORE_URL.format(plugin_id=urllib.parse.quote(plugin_id, safe=""))


def normalize_community_plugin(raw_plugin: dict[str, Any], idx: int) -> dict[str, Any] | None:
    plugin_id = str(raw_plugin.get("id", "")).strip()
    name = str(raw_plugin.get("name", "")).strip() or plugin_id
    if not plugin_id or not name:
        return None

    repo = str(raw_plugin.get("repo", "")).strip()
    return {
        "catalog_id": idx,
        "id": plugin_id,
        "name": name,
        "author": str(raw_plugin.get("author", "")).strip(),
        "description": str(raw_plugin.get("description", "")).strip(),
        "repo": repo,
        "plugin_url": build_plugin_store_url(plugin_id),
        "repo_url": f"https://github.com/{repo}" if repo else "",
    }


def load_cached_plugin_index() -> list[dict[str, Any]] | None:
    if not PLUGIN_INDEX_FILE.exists():
        return None
    try:
        payload = json.loads(PLUGIN_INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not payload.get("plugins"):
        return None
    if not is_cache_fresh(payload, cfg.obsidian_community_plugins_cache_ttl_hours):
        return None
    return payload["plugins"]


def load_any_cached_plugin_index() -> list[dict[str, Any]] | None:
    if not PLUGIN_INDEX_FILE.exists():
        return None
    try:
        payload = json.loads(PLUGIN_INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    plugins = payload.get("plugins")
    return plugins if plugins else None


async def fetch_help_index() -> list[dict[str, Any]]:
    cached_docs = load_cached_index()
    if cached_docs:
        return cached_docs

    async with INDEX_LOCK:
        cached_docs = load_cached_index()
        if cached_docs:
            return cached_docs

        base_url = cfg.obsidian_help_base_url.rstrip("/")
        sitemap_url = f"{base_url}/sitemap.xml"
        try:
            async with httpx.AsyncClient(**build_client_kwargs()) as client:
                response = await client.get(sitemap_url)
                response.raise_for_status()
        except httpx.ConnectError as exc:
            stale_docs = load_any_cached_index()
            if stale_docs:
                logger.warning("Using stale Obsidian help index cache because sitemap is unreachable.")
                return stale_docs
            logger.exception("Obsidian sitemap connect error: %s", exc)
            if cfg.obsidian_proxy:
                raise RuntimeError(
                    "连接 Obsidian 帮助文档失败。请检查 `OBSIDIAN_PROXY` 是否可用。"
                ) from exc
            raise RuntimeError(
                "连接 Obsidian 帮助文档失败。请检查当前网络，或在 `.env` 中设置 `OBSIDIAN_PROXY`。"
            ) from exc
        except httpx.HTTPStatusError as exc:
            stale_docs = load_any_cached_index()
            if stale_docs:
                logger.warning(
                    "Using stale Obsidian help index cache because sitemap returned status %s.",
                    exc.response.status_code,
                )
                return stale_docs
            logger.exception("Obsidian sitemap http error: %s", exc)
            raise RuntimeError(
                f"读取 Obsidian 帮助文档索引失败，状态码：{exc.response.status_code}"
            ) from exc

        all_urls = parse_sitemap(response.text)
        docs = [
            url_to_doc(url, idx)
            for idx, url in enumerate(all_urls, start=1)
            if "/help" in urllib.parse.urlparse(url).path or url.startswith(base_url)
        ]
        if not docs:
            raise RuntimeError("没有从 Obsidian 帮助文档 sitemap 中解析到可用页面。")

        ensure_cache_dir()
        payload = {
            "updated_at": time.time(),
            "base_url": base_url,
            "docs": docs,
        }
        INDEX_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return docs


async def fetch_community_plugin_index() -> list[dict[str, Any]]:
    cached_plugins = load_cached_plugin_index()
    if cached_plugins:
        return cached_plugins

    async with PLUGIN_INDEX_LOCK:
        cached_plugins = load_cached_plugin_index()
        if cached_plugins:
            return cached_plugins

        try:
            async with httpx.AsyncClient(**build_client_kwargs()) as client:
                response = await client.get(cfg.obsidian_community_plugins_json_url)
                response.raise_for_status()
                raw_plugins = response.json()
        except httpx.ConnectError as exc:
            stale_plugins = load_any_cached_plugin_index()
            if stale_plugins:
                logger.warning(
                    "Using stale Obsidian community plugin cache because directory is unreachable."
                )
                return stale_plugins
            logger.exception("Obsidian community plugin index connect error: %s", exc)
            if cfg.obsidian_proxy:
                raise RuntimeError(
                    "连接 Obsidian 社区插件目录失败。请检查 `OBSIDIAN_PROXY` 是否可用。"
                ) from exc
            raise RuntimeError(
                "连接 Obsidian 社区插件目录失败。请检查当前网络，或在 `.env` 中设置 `OBSIDIAN_PROXY`。"
            ) from exc
        except httpx.HTTPStatusError as exc:
            stale_plugins = load_any_cached_plugin_index()
            if stale_plugins:
                logger.warning(
                    "Using stale Obsidian community plugin cache because directory returned status %s.",
                    exc.response.status_code,
                )
                return stale_plugins
            logger.exception("Obsidian community plugin index http error: %s", exc)
            raise RuntimeError(
                f"读取 Obsidian 社区插件目录失败，状态码：{exc.response.status_code}"
            ) from exc
        except (TypeError, ValueError) as exc:
            logger.exception("Obsidian community plugin index parse error: %s", exc)
            raise RuntimeError("Obsidian 社区插件目录返回格式异常。") from exc

        if not isinstance(raw_plugins, list):
            raise RuntimeError("Obsidian 社区插件目录返回格式异常。")

        plugins = [
            plugin
            for idx, raw_plugin in enumerate(raw_plugins, start=1)
            if (plugin := normalize_community_plugin(raw_plugin, idx)) is not None
        ]
        if not plugins:
            raise RuntimeError("没有从 Obsidian 社区插件目录中解析到可用插件。")

        ensure_cache_dir()
        payload = {
            "updated_at": time.time(),
            "plugins": plugins,
        }
        PLUGIN_INDEX_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return plugins


def compact_doc_catalog(docs: list[dict[str, Any]]) -> str:
    return "\n".join(
        f'{doc["id"]}. {doc["title"]} | {doc["slug"]} | {doc["url"]}'
        for doc in docs
    )


def compact_plugin_catalog(plugins: list[dict[str, Any]]) -> str:
    return "\n".join(
        (
            f'{plugin["catalog_id"]}. {plugin["name"]} | '
            f'{plugin["id"]} | '
            f'author: {plugin["author"] or "unknown"} | '
            f'description: {plugin["description"] or "no description"} | '
            f'plugin page: {plugin["plugin_url"]}'
        )
        for plugin in plugins
    )


def extract_json_object(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model output")
    return json.loads(match.group(0))


def fallback_rank_docs(question: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keywords = [token for token in re.split(r"[\s,，。！？/]+", question.lower()) if token]

    def score(doc: dict[str, Any]) -> int:
        haystack = f'{doc["title"]} {doc["slug"]}'.lower()
        return sum(1 for token in keywords if token in haystack)

    ranked = sorted(docs, key=score, reverse=True)
    count = max(1, min(cfg.obsidian_help_max_candidates, len(ranked)))
    return ranked[:count]


def fallback_plugin_search_terms(question: str) -> list[str]:
    terms = [token.strip().lower() for token in re.split(r"[\s,，。！？、:：/()]+", question) if token]
    deduped: list[str] = []
    for term in terms:
        if term and term not in deduped:
            deduped.append(term)
    return deduped[:8]


def score_plugin(plugin: dict[str, Any], terms: list[str]) -> int:
    name = plugin["name"].lower()
    plugin_id = plugin["id"].lower()
    author = plugin["author"].lower()
    description = plugin["description"].lower()
    repo = plugin["repo"].lower()
    haystack = " ".join(part for part in (name, plugin_id, author, description, repo) if part)

    score = 0
    for term in terms:
        normalized = term.strip().lower()
        if len(normalized) < 2:
            continue
        if normalized in name:
            score += 8
        if normalized in plugin_id:
            score += 7
        if normalized in description:
            score += 6
        if normalized in author:
            score += 3
        if normalized in repo:
            score += 3

        words = [word for word in re.split(r"[\s/_-]+", normalized) if len(word) >= 2]
        for word in words:
            if word in name:
                score += 4
            elif word in description:
                score += 2
            elif word in haystack:
                score += 1

    return score


def fallback_rank_plugins(plugins: list[dict[str, Any]], terms: list[str]) -> list[dict[str, Any]]:
    ranked = sorted(
        plugins,
        key=lambda plugin: (score_plugin(plugin, terms), plugin["name"].lower()),
        reverse=True,
    )
    return [plugin for plugin in ranked if score_plugin(plugin, terms) > 0][:30]


async def call_ai(messages: list[dict[str, str]], temperature: float = 0.2) -> str:
    headers = {
        "Authorization": f"Bearer {cfg.obsidian_ai_api_key}",
        "Content-Type": "application/json",
    }
    data = {
        "model": cfg.obsidian_ai_model,
        "messages": messages,
        "temperature": temperature,
    }
    api_url = cfg.obsidian_ai_base_url.rstrip("/") + "/chat/completions"

    try:
        async with httpx.AsyncClient(**build_client_kwargs()) as client:
            response = await client.post(api_url, json=data, headers=headers)
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"].strip()
    except httpx.ConnectError as exc:
        logger.exception("Obsidian AI connect error: %s", exc)
        if cfg.obsidian_proxy:
            raise RuntimeError(
                "连接 AI 接口失败。请检查 `OBSIDIAN_AI_BASE_URL` 和 `OBSIDIAN_PROXY` 配置是否正确。"
            ) from exc
        raise RuntimeError("连接 AI 接口失败。请检查 `OBSIDIAN_AI_BASE_URL` 是否可访问。") from exc
    except httpx.HTTPStatusError as exc:
        logger.exception("Obsidian AI http error: %s", exc)
        raise RuntimeError(f"AI 接口返回异常状态码：{exc.response.status_code}") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.exception("Obsidian AI response parse error: %s", exc)
        raise RuntimeError("AI 接口返回格式异常，请检查接口是否兼容 OpenAI `chat/completions`。") from exc


async def generate_plugin_search_terms(question: str) -> list[str]:
    prompt = f"""
你要把用户的 Obsidian 使用需求，转成适合检索 Obsidian 社区插件目录的短搜索词。

用户问题：
{question}

输出要求：
1. 优先输出英文关键词或短语，因为社区插件目录大多是英文描述。
2. 关键词要偏功能名，不要写完整句子。
3. 如果用户已经提到了具体插件名，也要保留。
4. 最多返回 6 个词或短语。
5. 只输出 JSON，不要解释。
6. 格式必须是：{{"terms":["calendar","task management","kanban"]}}
""".strip()

    try:
        text = await call_ai(
            [
                {"role": "system", "content": "你负责为 Obsidian 社区插件目录生成检索关键词。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        payload = extract_json_object(text)
        raw_terms = payload.get("terms", [])
        if not isinstance(raw_terms, list):
            raise ValueError("terms is not a list")

        terms: list[str] = []
        for item in raw_terms:
            if not isinstance(item, str):
                continue
            normalized = item.strip().lower()
            if normalized and normalized not in terms:
                terms.append(normalized)
        if terms:
            return terms[:6]
    except Exception as exc:
        logger.warning("Falling back to local plugin search terms because AI extraction failed: %s", exc)

    return fallback_plugin_search_terms(question)


async def select_candidate_docs(question: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog = compact_doc_catalog(docs)
    max_candidates = max(1, min(cfg.obsidian_help_max_candidates, 5))
    prompt = f"""
你是 Obsidian 官方帮助文档检索助手。
用户的问题是：{question}

下面是 Obsidian Help 的页面目录。请从中挑选最相关的 {max_candidates} 个页面。
要求：
1. 优先选择真正能回答问题的页面，不要只看单词表面相似。
2. 允许根据中文问题推断对应的英文术语。
3. 只输出 JSON，不要输出解释。
4. 格式必须是：{{"ids":[1,2,3]}}

页面目录：
{catalog}
""".strip()

    try:
        text = await call_ai(
            [
                {"role": "system", "content": "你负责从文档目录中挑选最相关的页面。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        payload = extract_json_object(text)
        ids = payload.get("ids", [])
        if not isinstance(ids, list):
            raise ValueError("ids is not a list")
        selected_ids = {int(item) for item in ids[:max_candidates]}
        selected_docs = [doc for doc in docs if doc["id"] in selected_ids]
        if selected_docs:
            return selected_docs
    except Exception as exc:
        logger.warning("Falling back to local ranking because AI selection failed: %s", exc)

    return fallback_rank_docs(question, docs)


async def select_candidate_plugins(
    question: str, plugins: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    search_terms = await generate_plugin_search_terms(question)
    shortlist = fallback_rank_plugins(plugins, search_terms)
    if not shortlist:
        return []

    max_candidates = max(1, min(cfg.obsidian_community_plugins_max_candidates, 5))
    prompt = f"""
你是 Obsidian 社区插件推荐助手。
用户的问题是：{question}

下面是根据需求初筛出来的一小批候选插件。请从中挑选最适合的 {max_candidates} 个。

要求：
1. 只有在插件名称或描述明显匹配需求时才选择。
2. 如果没有真正合适的插件，返回空列表。
3. 优先选择最贴近用户需求的插件，不要为了凑数量硬选。
4. 只输出 JSON，不要解释。
5. 格式必须是：{{"ids":[1,2,3]}}

候选插件目录：
{compact_plugin_catalog(shortlist)}
""".strip()

    try:
        text = await call_ai(
            [
                {"role": "system", "content": "你负责从 Obsidian 社区插件候选列表中挑选最相关的插件。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        payload = extract_json_object(text)
        ids = payload.get("ids", [])
        if not isinstance(ids, list):
            raise ValueError("ids is not a list")
        selected_ids = {int(item) for item in ids[:max_candidates]}
        selected_plugins = [plugin for plugin in shortlist if plugin["catalog_id"] in selected_ids]
        if selected_plugins:
            return selected_plugins
        if ids == []:
            return []
    except Exception as exc:
        logger.warning("Falling back to local plugin ranking because AI selection failed: %s", exc)

    return shortlist[:max_candidates]


def parse_help_page(html: str) -> tuple[str, str]:
    parser = HelpHTMLParser()
    parser.feed(html)
    parser.close()
    text = parser.text()
    title = parser.title()
    return title, text[:8000]


async def fetch_doc_context(doc: dict[str, Any]) -> dict[str, str]:
    try:
        async with httpx.AsyncClient(**build_client_kwargs()) as client:
            response = await client.get(doc["url"])
            response.raise_for_status()
    except httpx.ConnectError as exc:
        logger.exception("Obsidian page connect error: %s", exc)
        raise RuntimeError(f"读取文档页面失败：{doc['url']}") from exc
    except httpx.HTTPStatusError as exc:
        logger.exception("Obsidian page http error: %s", exc)
        raise RuntimeError(
            f"读取文档页面失败，状态码：{exc.response.status_code}，页面：{doc['url']}"
        ) from exc

    page_title, page_text = parse_help_page(response.text)
    return {
        "title": page_title or doc["title"],
        "url": doc["url"],
        "slug": doc["slug"],
        "content": page_text,
    }


def find_community_plugins_doc(docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    for doc in docs:
        haystack = f'{doc["title"]} {doc["slug"]}'.lower()
        if "community plugin" in haystack or "plugins/community-plugins" in haystack:
            return doc
    return None


def normalize_plain_text_output(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").strip()
    cleaned = re.sub(r"```[^\n]*\n?", "", cleaned)
    cleaned = cleaned.replace("```", "")
    cleaned = re.sub(r"^\s{0,3}#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*[-*+]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*(\d+)\.\s+", r"\1、", cleaned, flags=re.MULTILINE)
    cleaned = cleaned.replace("**", "")
    cleaned = cleaned.replace("__", "")
    cleaned = cleaned.replace("`", "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def split_forward_chunks(text: str, max_length: int = 800) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    if not paragraphs:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= max_length:
            current = paragraph
            continue
        start = 0
        while start < len(paragraph):
            chunks.append(paragraph[start : start + max_length].strip())
            start += max_length

    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk]


def get_sender_name(event: Event) -> str:
    sender = getattr(event, "sender", None)
    card = getattr(sender, "card", "") if sender else ""
    nickname = getattr(sender, "nickname", "") if sender else ""
    return str(card or nickname or "用户")


async def send_forward_response(
    bot: Any,
    event: Event,
    question: str,
    answer: str,
) -> bool:
    if not isinstance(bot, OneBotV11Bot):
        return False
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False

    answer_chunks = split_forward_chunks(answer)
    if not answer_chunks:
        return False

    nodes = [
        MessageSegment.node_custom(
            user_id=event.get_user_id(),
            nickname=get_sender_name(event),
            content=f"问题：{question}",
        )
    ]
    for index, chunk in enumerate(answer_chunks, start=1):
        content = chunk if index == 1 else f"续：\n{chunk}"
        nodes.append(
            MessageSegment.node_custom(
                user_id=bot.self_id,
                nickname="Obsidian 助手",
                content=content,
            )
        )

    if isinstance(event, GroupMessageEvent):
        await bot.send_group_forward_msg(group_id=event.group_id, messages=nodes)
        return True
    if isinstance(event, PrivateMessageEvent):
        await bot.send_private_forward_msg(user_id=event.user_id, messages=nodes)
        return True
    return False


async def answer_with_docs_and_plugins(
    question: str,
    contexts: list[dict[str, str]],
    plugins: list[dict[str, str]],
) -> str:
    docs_text = "\n\n".join(
        (
            f'文档标题：{item["title"]}\n'
            f'文档链接：{item["url"]}\n'
            f'文档路径：{item["slug"]}\n'
            f'文档内容：\n{item["content"]}'
        )
        for item in contexts
    )
    plugins_text = "\n\n".join(
        (
            f'插件名：{item["name"]}\n'
            f'插件 ID：{item["id"]}\n'
            f'作者：{item["author"] or "unknown"}\n'
            f'插件描述：{item["description"] or "no description"}\n'
            f'插件页：{item["plugin_url"]}\n'
            f'仓库：{item["repo_url"] or "unknown"}'
        )
        for item in plugins
    )
    prompt = f"""
你是 Obsidian 助手。请结合官方帮助文档和社区插件目录信息回答用户问题。

用户问题：
{question}

输出要求：
1. 用中文回答。
2. 用正常文本和自然段回答，不要使用 Markdown。
3. 不要使用标题符号、列表符号、代码块、加粗、反引号，也不要写成 Markdown 链接。
4. 优先使用官方文档解释 Obsidian 内置能力、设置方式和插件安装方法。
5. 如果社区插件更适合解决问题，可以推荐 1-{max(len(plugins), 1)} 个插件，但必须明确标注“这是社区插件，不是官方内置功能”。
6. 对社区插件只能基于提供的目录描述回答；如果信息不够，请明确说“从插件目录描述看，可能适合……，详细功能需要打开插件页确认”。
7. 如果官方文档已经能直接解决，就不要强行推荐插件。
8. 如果推荐插件，顺带提醒用户社区插件属于第三方代码，安装前可以看看插件页和仓库说明。
9. 相关链接直接用普通文本写出完整 URL；如果有推荐插件，再补充相关社区插件链接。
10. 不要编造不存在的插件或插件细节。

官方文档内容：
{docs_text}

社区插件目录信息：
{plugins_text or "本次没有找到明确匹配的社区插件候选。"}
""".strip()

    return await call_ai(
        [
            {"role": "system", "content": "你是一个擅长整理 Obsidian 官方文档和社区插件信息的助手。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
    )


@obsidian_matcher.handle()
async def handle_obsidian(bot: Bot, event: Event, msg: str = EventPlainText()) -> None:
    question = extract_obsidian_question(msg)
    if not question:
        await obsidian_matcher.finish("请用 `ob 你的问题` 的格式来问我，例如：`ob 怎么创建模板？`")

    if not cfg.obsidian_ai_api_key or not cfg.obsidian_ai_model or not cfg.obsidian_ai_base_url:
        await obsidian_matcher.finish(
            "Obsidian 助手还没配置好 AI 接口。请在 `.env` 中设置 "
            "`OBSIDIAN_AI_API_KEY`、`OBSIDIAN_AI_MODEL` 和 `OBSIDIAN_AI_BASE_URL`。"
        )

    await obsidian_matcher.send("正在理解你的问题，请稍候...")

    try:
        await obsidian_matcher.send("正在查找官方文档和社区插件目录...")
        docs_result, plugins_result = await asyncio.gather(
            fetch_help_index(),
            fetch_community_plugin_index(),
            return_exceptions=True,
        )
        if isinstance(docs_result, Exception):
            raise docs_result
        docs = docs_result
        if isinstance(plugins_result, Exception):
            logger.warning(
                "Community plugin directory is unavailable, continuing with official docs only: %s",
                plugins_result,
            )
            plugins: list[dict[str, Any]] = []
        else:
            plugins = plugins_result

        selected_docs = await select_candidate_docs(question, docs)
        selected_plugins = await select_candidate_plugins(question, plugins) if plugins else []

        if selected_plugins:
            community_plugins_doc = find_community_plugins_doc(docs)
            if community_plugins_doc and all(
                doc["id"] != community_plugins_doc["id"] for doc in selected_docs
            ):
                selected_docs.append(community_plugins_doc)

        contexts = await asyncio.gather(*(fetch_doc_context(doc) for doc in selected_docs))
        answer = await answer_with_docs_and_plugins(question, contexts, selected_plugins)
        answer = normalize_plain_text_output(answer)
        if not answer:
            answer = "我查到了相关资料，但这次没有整理出可发送的结果。你可以换个问法再试一次。"
    except RuntimeError as exc:
        await obsidian_matcher.finish(str(exc))
    except Exception as exc:
        logger.exception("Obsidian Helper Error: %s: %s", type(exc).__name__, exc)
        await obsidian_matcher.finish("抱歉，我处理这个问题时出了点问题，请稍后再试。")

    try:
        if await send_forward_response(bot, event, question, answer):
            return
    except Exception as exc:
        logger.warning("Failed to send forward message, falling back to plain text: %s", exc)

    await obsidian_matcher.finish(answer)
