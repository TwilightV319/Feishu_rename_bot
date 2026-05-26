from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata
from typing import Dict, Any
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, Bot
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="plus_one",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

# 存储各个群聊的状态
# key 为 group_id, value 为该群的消息状态
group_states: Dict[int, Dict[str, Any]] = {}

# 注册一个消息响应器，不设置命令，处理所有群消息
# priority 设置高一点（数字大一点），让其他指令类插件先处理
repeat_matcher = on_message(priority=99, block=False)

@repeat_matcher.handle()
async def handle_repeat(bot: Bot, event: GroupMessageEvent):
    group_id = event.group_id
    # 获取消息内容（包含图片、表情等，转为字符串进行比对）
    current_msg = event.get_message()
    # 转为字符串便于存储和对比
    current_msg_str = str(current_msg)
    
    # 获取该群的状态，如果没有则初始化
    if group_id not in group_states:
        group_states[group_id] = {
            "last_msg": current_msg_str,
            "count": 1,
            "bot_repeated": False
        }
        return

    state = group_states[group_id]

    # 判断当前消息是否与上一条相同
    if current_msg_str == state["last_msg"]:
        state["count"] += 1
        
        # 条件：出现两次且机器人本轮还没跟风
        if state["count"] == 2 and not state["bot_repeated"]:
            state["bot_repeated"] = True
            # 发送相同内容实现 +1
            # 注意：这里直接发送 Message 对象，可以复现图片和表情
            await repeat_matcher.send(current_msg)
    else:
        # 消息不同，重置该群状态
        state["last_msg"] = current_msg_str
        state["count"] = 1
        state["bot_repeated"] = False