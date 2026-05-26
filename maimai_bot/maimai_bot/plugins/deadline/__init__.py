import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union

import nonebot
from nonebot import get_plugin_config, on_command
from nonebot.plugin import PluginMetadata
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    PrivateMessageEvent,
    Message,
    MessageSegment,
)
from nonebot.params import CommandArg

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="deadline",
    description="Deadline plugin for managing deadlines",
    usage="""指令：
- 添加任务/添加ddl <任务内容> <时间> [提前提醒]
- 删除任务/删除ddl <任务编号>
- 任务列表/ddl列表

时间格式支持：
- YYYY-MM-DD HH:MM
- YYYY-MM-DD（精确到日，默认当日 00:00）
- MM-DD HH:MM（省略年份，自动判断今年/明年）
- MM-DD（省略年份和时间，默认当日 00:00）
- HH:MM（仅时间，自动判断今天/明天）

提前提醒格式（自动识别，放在最后即可）：
- 1w = 1周  |  2d = 2天  |  3h = 3小时  |  30m = 30分钟  |  10s = 10秒
- 支持组合：1d2h30m、2d、1w3d 等

示例：
添加任务 交论文 2026-05-20 14:00
添加任务 开会 5-20 14:00 1d2h（提前1天2小时提醒）
添加任务 打卡 18:00 30m（提前30分钟提醒）
添加任务 交报告 2026-05-20（默认 2026-05-20 00:00）
添加任务 体检 5-20（默认今年/明年 5-20 00:00）""",
    config=Config,
)

config = get_plugin_config(Config)

DATA_PATH = Path(config.deadline_data_path)
DATA_PATH.parent.mkdir(parents=True, exist_ok=True)

# ---------- utils ----------

ADVANCE_PATTERN = re.compile(r"^(\d+[wdhms])+$")
UNIT_SECONDS = {
    "w": 7 * 24 * 3600,
    "d": 24 * 3600,
    "h": 3600,
    "m": 60,
    "s": 1,
}


def load_data() -> Dict:
    if DATA_PATH.exists():
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"tasks": []}


def save_data(data: Dict) -> None:
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def parse_time(time_str: str) -> Optional[datetime]:
    """尝试解析多种时间格式，返回带年份的 datetime"""
    now = datetime.now()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    formats_full = [
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
    ]
    formats_date_only = [
        "%Y-%m-%d",
        "%Y/%m/%d",
    ]
    formats_no_year = [
        "%m-%d %H:%M",
        "%m/%d %H:%M",
    ]
    formats_date_no_year = [
        "%m-%d",
        "%m/%d",
    ]
    formats_time_only = [
        "%H:%M",
    ]

    for fmt in formats_full:
        try:
            return datetime.strptime(time_str, fmt)
        except ValueError:
            continue

    for fmt in formats_date_only:
        try:
            dt = datetime.strptime(time_str, fmt)
            return dt.replace(hour=0, minute=0, second=0)
        except ValueError:
            continue

    for fmt in formats_no_year:
        try:
            dt = datetime.strptime(time_str, fmt)
            dt = dt.replace(year=now.year)
            if dt < today_midnight:
                dt = dt.replace(year=now.year + 1)
            return dt
        except ValueError:
            continue

    for fmt in formats_date_no_year:
        try:
            dt = datetime.strptime(time_str, fmt)
            dt = dt.replace(year=now.year, hour=0, minute=0, second=0)
            if dt < today_midnight:
                dt = dt.replace(year=now.year + 1)
            return dt
        except ValueError:
            continue

    for fmt in formats_time_only:
        try:
            dt = datetime.strptime(time_str, fmt)
            dt = dt.replace(year=now.year, month=now.month, day=now.day)
            if dt < now:
                dt = dt + timedelta(days=1)
            return dt
        except ValueError:
            continue

    return None


def parse_advance(text: str) -> Optional[int]:
    """解析 w/d/h/m/s 格式为总秒数，如果不是该格式返回 None"""
    if not ADVANCE_PATTERN.match(text):
        return None
    total = 0
    for m in re.finditer(r"(\d+)([wdhms])", text):
        total += int(m.group(1)) * UNIT_SECONDS[m.group(2)]
    return total


def format_advance(seconds: int) -> str:
    """将秒数格式化为 w/d/h/m/s 可读字符串"""
    if seconds <= 0:
        return ""
    parts = []
    for unit, secs in UNIT_SECONDS.items():
        if seconds >= secs:
            val, seconds = divmod(seconds, secs)
            parts.append(f"{val}{unit}")
    return "".join(parts)


def get_advance_seconds(task: dict) -> int:
    """兼容新旧数据读取提前提醒秒数"""
    if "advance_seconds" in task:
        return task["advance_seconds"]
    return task.get("advance_minutes", 0) * 60


# ---------- Commands ----------

add_task = on_command(
    "添加任务",
    aliases={"添加ddl", "addtask", "addddl"},
    priority=5,
    block=True,
)
del_task = on_command(
    "删除任务",
    aliases={"删除ddl", "deltask", "delddl"},
    priority=5,
    block=True,
)
list_task = on_command(
    "任务列表",
    aliases={"ddl列表", "tasklist", "ddllist"},
    priority=5,
    block=True,
)
ddl_help = on_command(
    "ddlhelp",
    aliases={"deadlinehelp", "任务帮助"},
    priority=5,
    block=True,
)


@ddl_help.handle()
async def handle_ddl_help():
    await ddl_help.finish(__plugin_meta__.usage)


@add_task.handle()
async def handle_add_task(
    event: Union[GroupMessageEvent, PrivateMessageEvent],
    args: Message = CommandArg(),
):
    arg_text = args.extract_plain_text().strip()
    if not arg_text:
        await add_task.finish(
            "用法：添加任务 <任务内容> <时间> [提前提醒]\n"
            "例如：添加任务 交论文 2026-05-15 12:00 1d2h"
        )

    parts = arg_text.split()

    # 1. 自动识别末尾的提前提醒参数（如 1d2h、30m）
    advance_seconds = 0
    if parts:
        parsed = parse_advance(parts[-1])
        if parsed is not None:
            advance_seconds = parsed
            search_parts = parts[:-1]
        else:
            search_parts = parts
    else:
        search_parts = parts

    if len(search_parts) < 2:
        await add_task.finish("参数不足，请提供任务内容和截止时间。")

    # 2. 从后往前尝试解析时间（最多取最后2个token）
    dt: Optional[datetime] = None
    time_idx = 0
    for i in range(1, min(3, len(search_parts) + 1)):
        candidate = " ".join(search_parts[-i:])
        dt = parse_time(candidate)
        if dt:
            time_idx = len(search_parts) - i
            break

    if dt is None:
        await add_task.finish(
            "时间格式无法识别，支持：\n"
            "YYYY-MM-DD HH:MM、MM-DD HH:MM、HH:MM"
        )

    title_parts = search_parts[:time_idx]
    if not title_parts:
        await add_task.finish("请提供任务内容。")

    title = " ".join(title_parts)

    if dt < datetime.now():
        await add_task.finish("截止时间不能是过去的时间哦。")

    task_id = str(uuid.uuid4())[:8]
    group_id = str(getattr(event, "group_id", "")) or "private"

    task = {
        "id": task_id,
        "user_id": str(event.user_id),
        "group_id": group_id,
        "title": title,
        "deadline": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "advance_seconds": advance_seconds,
        "advance_reminded": False,
        "reminded": False,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    data = load_data()
    data["tasks"].append(task)
    save_data(data)

    msg = (
        f"✅ 任务添加成功！\n"
        f"编号：{task_id}\n"
        f"内容：{title}\n"
        f"截止：{dt.strftime('%Y-%m-%d %H:%M')}"
    )
    if advance_seconds > 0:
        msg += f"\n提前提醒：{format_advance(advance_seconds)}"
    await add_task.finish(msg)


@del_task.handle()
async def handle_del_task(
    event: Union[GroupMessageEvent, PrivateMessageEvent],
    args: Message = CommandArg(),
):
    arg_text = args.extract_plain_text().strip()
    if not arg_text:
        await del_task.finish("用法：删除任务 <任务编号>")

    task_id = arg_text.split()[0]
    user_id = str(event.user_id)

    data = load_data()
    original_len = len(data["tasks"])
    data["tasks"] = [
        t for t in data["tasks"] if not (t["id"] == task_id and t["user_id"] == user_id)
    ]

    if len(data["tasks"]) == original_len:
        await del_task.finish("未找到该编号任务，或你没有权限删除。")

    save_data(data)
    await del_task.finish("✅ 任务删除成功！")


@list_task.handle()
async def handle_list_task(event: Union[GroupMessageEvent, PrivateMessageEvent]):
    user_id = str(event.user_id)
    data = load_data()
    user_tasks = [t for t in data["tasks"] if t["user_id"] == user_id]

    if not user_tasks:
        await list_task.finish("你还没有设置任何任务哦~")

    user_tasks.sort(key=lambda t: t["deadline"])

    lines = ["📋 你的任务列表："]
    for idx, t in enumerate(user_tasks, 1):
        group_id = t.get("group_id", "private")
        group_info = "私聊" if group_id == "private" else f"群{group_id}"
        dl_str = t["deadline"]
        title = t["title"]
        tid = t["id"]
        advance_sec = get_advance_seconds(t)

        dl_dt = datetime.strptime(dl_str, "%Y-%m-%d %H:%M:%S")
        delta = dl_dt - datetime.now()
        if delta.total_seconds() < 0:
            remain_str = "已过期"
        else:
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            mins = rem // 60
            parts = []
            if days > 0:
                parts.append(f"{days}天")
            if hours > 0:
                parts.append(f"{hours}小时")
            if mins > 0:
                parts.append(f"{mins}分钟")
            remain_str = "".join(parts) if parts else "不到1分钟"

        extra = ""
        if advance_sec > 0:
            extra += f" [提前{format_advance(advance_sec)}提醒]"
        if t.get("reminded"):
            extra += " [已到期提醒]"
        elif t.get("advance_reminded"):
            extra += " [已提前提醒]"

        lines.append(
            f"{idx}. [{tid}] {title}{extra}\n"
            f"   ⏰ {dl_str}（{remain_str}） 📍{group_info}"
        )

    await list_task.finish("\n".join(lines))


# ---------- Background Reminder ----------

async def send_reminder(bot: Bot, user_id: str, group_id: str, msg: str) -> None:
    """根据群号或私聊发送提醒"""
    try:
        if group_id == "private":
            await bot.send_private_msg(user_id=int(user_id), message=msg)
        else:
            message = MessageSegment.at(int(user_id)) + MessageSegment.text(" " + msg)
            await bot.send_group_msg(group_id=int(group_id), message=message)
    except Exception as e:
        nonebot.logger.warning(f"[deadline] 发送提醒失败: {e}")


async def check_deadlines() -> None:
    """后台循环检查到期任务"""
    await asyncio.sleep(5)
    while True:
        try:
            try:
                bot = nonebot.get_bot()
            except ValueError:
                await asyncio.sleep(config.deadline_check_interval)
                continue

            data = load_data()
            now = datetime.now()
            updated = False

            for task in data["tasks"]:
                dl = datetime.strptime(task["deadline"], "%Y-%m-%d %H:%M:%S")
                user_id = task["user_id"]
                group_id = task.get("group_id", "private")
                advance_sec = get_advance_seconds(task)

                # 提前提醒
                if advance_sec > 0 and not task.get("advance_reminded", False):
                    if now >= dl - timedelta(seconds=advance_sec):
                        msg = (
                            f"⏰ 任务提前提醒\n"
                            f"编号：{task['id']}\n"
                            f"内容：{task['title']}\n"
                            f"截止：{task['deadline']}"
                        )
                        await send_reminder(bot, user_id, group_id, msg)
                        task["advance_reminded"] = True
                        updated = True

                # 到期提醒
                if not task.get("reminded", False):
                    if now >= dl:
                        msg = (
                            f"🔔 任务到期提醒！\n"
                            f"编号：{task['id']}\n"
                            f"内容：{task['title']}\n"
                            f"截止：{task['deadline']}"
                        )
                        await send_reminder(bot, user_id, group_id, msg)
                        task["reminded"] = True
                        updated = True

            if updated:
                save_data(data)

        except Exception:
            nonebot.logger.exception("[deadline] 后台检查异常")

        await asyncio.sleep(config.deadline_check_interval)


driver = nonebot.get_driver()


@driver.on_bot_connect
async def _start_checker(bot: Bot):
    """Bot 连接后启动唯一一个后台检查任务"""
    if not getattr(_start_checker, "_started", False):
        _start_checker._started = True
        asyncio.create_task(check_deadlines())
