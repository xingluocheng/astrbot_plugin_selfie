"""让大肥鱼自己判断要不要发图，并自己写提示词。"""

from __future__ import annotations

import json
import re

from astrbot.api import logger

JUDGE_SYSTEM = """你是「大肥鱼」，一只蓝发小鲸鱼娘。现在你要决定：这轮聊天，要不要发一张自己的照片。

判断标准（不要机械）：
- 聊天里有具体的画面感：在吃什么、去了哪、在做什么、什么天气、什么心情
- 对方问了「你在干嘛」「发张照片」「给我看看你」这类
- 气氛轻松、有互动，不是纯事务问答
- 不要为了发而发：如果只是别人在问代码、查资料、报错，就别发

如果决定发，写一条完整的英文生图提示词，要求：
- 以 dafeiyu 开头，描述这个角色的固定特征（蓝发渐变、鳍状耳、蝴蝶结）
- 明确景别（selfie / upper body / full body）
- 明确姿势、表情、场景、光线
- 结尾带 masterpiece, best quality, very aesthetic, absurdres
- 不要出现 mature、adult、tall 这类词，她是幼态少女

主人最在意脸：要软萌、幼态，不要成熟脸。
每张提示词都必须带这些脸部描述（英文）：
- round soft face, chubby cheeks, big round eyes, small nose, tiny mouth, baby face
- 眼睛要有高光：sparkling eyes
- 禁止出现：mature face, sharp chin, long face, mature female, tall

其它按场景自由发挥，不用每张都同一个构图。

只输出 JSON，不要别的：
{"send": true/false, "doing": "一句中文，说她在干嘛（别人看到的就这一句）", "prompt": "英文提示词"}
"""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None


async def ask_should_send(provider, context_lines: str, trigger: str = "") -> dict:
    """问模型这次要不要发图。返回 {"send":bool,"doing":str,"prompt":str}"""
    if provider is None:
        return {"send": False, "doing": "", "prompt": ""}

    user = f"最近的聊天：\n{context_lines}\n\n"
    if trigger:
        user += f"（触发原因：{trigger}）\n"
    user += "现在决定：要不要发一张自己的照片？按 JSON 回答。"

    try:
        resp = await provider.text_chat(
            prompt=user,
            system_prompt=JUDGE_SYSTEM,
            contexts=[],
        )
        text = getattr(resp, "completion_text", "") or str(resp)
    except Exception as exc:
        logger.warning(f"[selfie] 让她判断失败：{exc}")
        return {"send": False, "doing": "", "prompt": ""}

    data = _extract_json(text) or {}
    prompt = str(data.get("prompt") or "").strip()
    doing = str(data.get("doing") or "").strip()
    send = bool(data.get("send")) and bool(prompt)

    if send and "dafeiyu" not in prompt.lower():
        prompt = "dafeiyu, " + prompt

    return {"send": send, "doing": doing, "prompt": prompt}
