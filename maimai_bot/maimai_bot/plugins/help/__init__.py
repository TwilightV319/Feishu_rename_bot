import json
import re
from pathlib import Path
from typing import Any

from nonebot import get_plugin_config, logger, on_message
from nonebot.adapters import Event
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="help",
    description="可配置的帮助菜单",
    usage="help / 帮助 [功能名]",
    config=Config,
)

config = get_plugin_config(Config)
HELP_CONFIG_PATH = Path(config.help_config_path)


def build_default_help_data() -> dict[str, Any]:
    return {
        "bot_name": "maimai_bot",
        "intro": "这里是机器人的帮助菜单。你可以编辑 data/help/help.json，自定义功能简介和指令列表。",
        "footer": "发送“帮助 功能名”可以查看某个功能的详细说明。",
        "sections": [
            {
                "name": "Obsidian 助手",
                "aliases": ["obsidian", "ob"],
                "summary": "查询 Obsidian 官方帮助文档和社区插件信息。",
                "details": "适合快速查 Obsidian 用法、概念说明和插件资料。",
                "commands": [
                    {
                        "command": "ob <问题>",
                        "description": "向 Obsidian 帮助文档提问。",
                        "example": "ob dataview 怎么筛选任务",
                    }
                ],
            },
            {
                "name": "复读 +1",
                "aliases": ["plus_one", "复读"],
                "summary": "群里连续出现两次相同消息时，机器人会跟一次。",
                "details": "这是一个被动功能，不需要手动触发指令。",
                "commands": [],
            },
            {
                "name": "自定义功能示例",
                "aliases": ["示例"],
                "summary": "这个模块专门留给你自己填写。",
                "details": "你可以继续新增模块，或者修改这个示例模块的名称、简介和命令。",
                "commands": [
                    {
                        "command": "示例指令",
                        "description": "这里写指令用途。",
                        "example": "示例指令 参数",
                    }
                ],
            },
            {
                "name": "Deadline 任务提醒",
                "aliases": ["ddl", "deadline", "任务"],
                "summary": "设置任务截止提醒，支持提前通知，按群聊隔离。",
                "details": "可在任意群聊或私聊中设置任务，机器人会在截止时自动提醒。提前提醒支持 w/d/h/m/s 格式组合。时间只写日期则默认当日 00:00。",
                "commands": [
                    {
                        "command": "添加任务 <内容> <时间> [提前提醒]",
                        "description": "添加一个新任务，时间支持 YYYY-MM-DD HH:MM、YYYY-MM-DD、MM-DD、HH:MM",
                        "example": "添加任务 交论文 2026-05-20 14:00 1d2h",
                    },
                    {
                        "command": "删除任务 <编号>",
                        "description": "删除指定任务",
                        "example": "删除任务 a1b2c3d4",
                    },
                    {
                        "command": "任务列表 / ddl列表",
                        "description": "查看自己在所有群聊和私聊中的任务",
                        "example": "ddl列表",
                    },
                    {
                        "command": "ddlhelp",
                        "description": "查看 Deadline 插件的详细帮助",
                    },
                ],
            },
        ],
    }


def ensure_help_config() -> None:
    HELP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if HELP_CONFIG_PATH.exists():
        return
    HELP_CONFIG_PATH.write_text(
        json.dumps(build_default_help_data(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_help_data() -> dict[str, Any]:
    ensure_help_config()
    try:
        payload = json.loads(HELP_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"帮助配置文件读取失败，请检查 {HELP_CONFIG_PATH} 是否是合法 JSON。"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError("帮助配置文件格式错误，根节点必须是对象。")
    return payload


def normalize_command_item(item: Any) -> dict[str, str] | None:
    if isinstance(item, str):
        command = item.strip()
        return {"command": command} if command else None

    if not isinstance(item, dict):
        return None

    command = str(item.get("command", "")).strip()
    if not command:
        return None

    normalized: dict[str, str] = {"command": command}
    for key in ("description", "example", "note"):
        value = str(item.get(key, "")).strip()
        if value:
            normalized[key] = value
    return normalized


def normalize_section(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None

    name = str(item.get("name", "")).strip()
    if not name:
        return None

    raw_aliases = item.get("aliases", [])
    aliases = []
    if isinstance(raw_aliases, list):
        aliases = [str(alias).strip() for alias in raw_aliases if str(alias).strip()]

    raw_commands = item.get("commands", [])
    commands = []
    if isinstance(raw_commands, list):
        commands = [
            normalized
            for command in raw_commands
            if (normalized := normalize_command_item(command)) is not None
        ]

    return {
        "name": name,
        "aliases": aliases,
        "summary": str(item.get("summary", "")).strip(),
        "details": str(item.get("details", "")).strip(),
        "commands": commands,
    }


def get_sections(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_sections = payload.get("sections", [])
    if not isinstance(raw_sections, list):
        return []

    return [
        section
        for item in raw_sections
        if (section := normalize_section(item)) is not None
    ]


def extract_help_query(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None

    for command in config.help_commands:
        alias = command.strip()
        if not alias:
            continue
        match = re.match(rf"(?is)^{re.escape(alias)}(?:\s+(.+))?$", stripped)
        if match:
            return (match.group(1) or "").strip()
    return None


async def starts_with_help(event: Event) -> bool:
    return extract_help_query(event.get_plaintext()) is not None


def find_section(query: str, sections: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not query:
        return None

    if query.isdigit():
        index = int(query) - 1
        if 0 <= index < len(sections):
            return sections[index]

    normalized_query = query.casefold()
    for section in sections:
        names = [section["name"], *section["aliases"]]
        if any(normalized_query == name.casefold() for name in names):
            return section

    for section in sections:
        names = [section["name"], *section["aliases"]]
        if any(normalized_query in name.casefold() for name in names):
            return section

    return None


def format_overview(payload: dict[str, Any], sections: list[dict[str, Any]]) -> str:
    bot_name = str(payload.get("bot_name", "")).strip() or "机器人"
    intro = str(payload.get("intro", "")).strip()
    footer = str(payload.get("footer", "")).strip()

    lines = [f"{bot_name} 帮助"]
    if intro:
        lines.extend(["", intro])

    if sections:
        lines.extend(["", "指令使用列表："])
        for index, section in enumerate(sections, start=1):
            summary = section["summary"] or "暂无简介"
            lines.append(f"{index}. {section['name']}：{summary}")

            commands = section["commands"]
            if commands:
                for command in commands:
                    description = command.get("description", "")
                    line = f"   - {command['command']}"
                    if description:
                        line = f"{line}：{description}"
                    lines.append(line)

                    example = command.get("example", "")
                    if example:
                        lines.append(f"     例如：{example}")
            else:
                lines.append("   - 暂无可主动触发的指令")
    else:
        lines.extend(["", "当前还没有配置任何功能模块。"])

    if footer:
        lines.extend(["", footer])

    return "\n".join(lines)


def format_section_detail(section: dict[str, Any]) -> str:
    lines = [section["name"]]

    if section["summary"]:
        lines.extend(["", f"简介：{section['summary']}"])

    if section["details"]:
        lines.extend(["", section["details"]])

    commands = section["commands"]
    if commands:
        lines.extend(["", "指令列表："])
        for index, command in enumerate(commands, start=1):
            description = command.get("description", "")
            line = f"{index}. {command['command']}"
            if description:
                line = f"{line} - {description}"
            lines.append(line)

            example = command.get("example", "")
            if example:
                lines.append(f"   例如：{example}")

            note = command.get("note", "")
            if note:
                lines.append(f"   备注：{note}")
    else:
        lines.extend(["", "这个功能暂未配置指令列表。"])

    aliases = section["aliases"]
    if aliases:
        lines.extend(["", f"别名：{', '.join(aliases)}"])

    return "\n".join(lines)


help_matcher = on_message(
    rule=Rule(starts_with_help),
    priority=config.help_priority,
    block=config.help_block,
)


@help_matcher.handle()
async def handle_help(event: Event) -> None:
    query = extract_help_query(event.get_plaintext())
    if query is None:
        return

    try:
        payload = load_help_data()
    except RuntimeError as exc:
        logger.warning(str(exc))
        await help_matcher.finish(str(exc))

    sections = get_sections(payload)
    if not query:
        await help_matcher.finish(format_overview(payload, sections))

    section = find_section(query, sections)
    if section is None:
        await help_matcher.finish(
            f"没有找到“{query}”这个功能。\n发送“帮助”查看全部模块，或检查 {HELP_CONFIG_PATH} 中的名称和别名配置。"
        )

    await help_matcher.finish(format_section_detail(section))
