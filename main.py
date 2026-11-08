"""astrbot_plugin_selfie —— 大肥鱼自己决定发不发照片。

日常聊天里她自己判断有没有画面感、想不想发一张；要发就自己写提示词，
交给本地 ComfyUI 出图，然后把图发出来。
"""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools

from . import comfy, judge
from .storage import State

TAG = "[selfie]"
DEFAULT_CHECK_EVERY = 10
DEFAULT_COOLDOWN = 30


class SelfiePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir: Path = StarTools.get_data_dir("astrbot_plugin_selfie")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.image_dir = self.data_dir / "images"
        self.state = State(self.data_dir)
        self._buffers: dict[str, list[str]] = {}
        self._our_images: dict[str, set[str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        logger.info(f"{TAG} 已加载，第 {self._cfg('lora_step', 12)} 档 / 权重 {self._cfg('lora_strength', 0.7)}")

    # ------------------------------------------------------------- 配置
    def _cfg(self, key: str, default=None):
        value = self.config.get(key, default) if isinstance(self.config, dict) else default
        return default if value is None or value == "" else value

    def _owner_ids(self) -> set[str]:
        raw = str(self._cfg("owner_ids", "") or "")
        return {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            sid = str(event.get_sender_id())
        except Exception:
            return False
        return sid in self._owner_ids()

    def _provider(self):
        try:
            return self.context.get_using_provider()
        except Exception as exc:
            logger.debug(f"{TAG} 取模型失败：{exc}")
            return None

    def _lock(self, umo: str) -> asyncio.Lock:
        if umo not in self._locks:
            self._locks[umo] = asyncio.Lock()
        return self._locks[umo]

    # ------------------------------------------------------------- 观察
    @filter.event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not bool(self._cfg("enabled", True)):
            return
        text = (event.message_str or "").strip()
        if not text or text.startswith("/"):
            return
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return
        except Exception:
            pass

        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return

        # 只用指令模式：平时聊天不自动拍图（也就不动表情包）
        if not bool(self._cfg("auto_enabled", False)):
            return

        buf = self._buffers.setdefault(umo, [])
        buf.append(text[:200])
        del buf[:-20]

        count = self.state.bump(umo)
        every = int(self._cfg("selfie_check_every", DEFAULT_CHECK_EVERY) or DEFAULT_CHECK_EVERY)

        owner_direct = self._is_owner(event) and self._looks_like_request(text)
        if not owner_direct and count < every:
            return
        self.state.reset_counter(umo)

        # 群聊没开就只在私聊里发
        if not bool(self._cfg("group_enabled", False)) and not event.is_private_chat():
            return
        cooldown = int(self._cfg("cooldown_minutes", DEFAULT_COOLDOWN) or 0)
        if not owner_direct and self.state.in_cooldown(cooldown):
            return

        verdict = await self._judge(event, umo, owner_direct)
        if not verdict or not verdict.get("send"):
            return
        if not owner_direct and random.random() > float(self._cfg("send_probability", 0.8) or 0.8):
            logger.debug(f"{TAG} 判定成立但随机跳过")
            return

        if self.state.has_flag(umo, "busy", ttl=240):
            logger.debug(f"{TAG} 正在拍图，跳过本轮自动出图")
            return

        async with self._lock(umo):
            if not owner_direct and self.state.in_cooldown(cooldown):
                return
            self.state.set_flag(umo, "busy")
            try:
                path = await self.make_image(verdict["prompt"], tag="chat")
            except comfy.ComfyError as exc:
                logger.warning(f"{TAG} 出图失败：{exc}")
                return
            finally:
                self.state.clear_flag(umo, "busy")
            self.state.mark(doing=verdict.get("doing", ""), path=str(path))
            self.state.set_flag(umo, "sent")
            self._our_images.setdefault(umo, set()).add(str(path.name))
            logger.info(f"{TAG} 发图：{verdict.get('doing','')}")

        doing = verdict.get("doing") or "刚拍了张照"
        self._tell_diary(event, umo, doing)
        yield event.chain_result([Plain(doing), Image.fromFileSystem(str(path))])

        if bool(self._cfg("notify_owner", False)):
            await self._notify_owner(event, path, doing)

    # ------------------------------------------------------------- 日记联动
    def _tell_diary(self, event: AstrMessageEvent, umo: str, doing: str) -> None:
        """把「她拍了什么」告诉日记插件，让日记里能写上这件事。"""
        try:
            star = self.context.get_registered_star("astrbot_plugin_diary")
        except Exception:
            return
        if star is None:
            return
        plugin = getattr(star, "star_cls", None) or star
        fn = getattr(plugin, "record_selfie", None)
        if fn is None:
            return
        try:
            uid = str(event.get_sender_id() or "")
            fn(umo, uid, doing)
        except Exception as exc:
            logger.debug(f"{TAG} 通知日记失败：{exc}")
    # ------------------------------------------------------------- 判断
    async def _judge(self, event: AstrMessageEvent, umo: str, owner_direct: bool,
                     requirement: str = "") -> dict | None:
        lines = self._buffers.get(umo, [])[-12:]
        context_lines = "\n".join(f"- {x}" for x in lines) or "（暂时没什么聊天内容）"
        trigger = "主人直接要照片" if owner_direct else ""
        verdict = await judge.ask_should_send(
            self._provider(), context_lines, trigger, requirement
        )
        if verdict.get("send"):
            logger.info(f"{TAG} 判定发图：{verdict.get('doing','')} | {verdict['prompt'][:60]}")
        return verdict

    @staticmethod
    def _looks_like_request(text: str) -> bool:
        words = ("照片", "自拍", "发张", "发个图", "给我看", "看看你", "画一张", "生成", "来一张")
        return any(w in text for w in words)

    # ------------------------------------------------------------- 出图
    async def make_image(self, prompt: str, tag: str = "selfie",
                         strength: float = 0.0) -> Path:
        """生成一张图，返回本地路径。qzone 插件也调这个。

        strength > 0 时覆盖配置里的 LoRA 权重（换服装时压低，减轻训练集惯性）。
        """
        face = str(self._cfg("face_hint", "") or "").strip()
        if face and face.split(",")[0].strip().lower() not in prompt.lower():
            prompt = f"{prompt}, {face}"
        hint = str(self._cfg("style_hint", "") or "").strip()
        if hint:
            prompt = f"{prompt}, {hint}"
        extra = str(self._cfg("extra_tags", "") or "").strip()
        if extra:
            prompt = f"{prompt}, {extra}"
        raw = await asyncio.to_thread(
            comfy.generate,
            str(self._cfg("bridge_url", "http://127.0.0.1:8190")),
            prompt,
            str(self._cfg("size", "832x1152")),
            int(self._cfg("steps", 28)),
            float(self._cfg("cfg", 5.5)),
            str(self._cfg("model_name", "dafeiyu")),
            float(strength),
        )
        path = comfy.save(raw, self.image_dir, tag=tag)
        keep = int(self._cfg("max_keep_images", 200) or 0)
        removed = comfy.prune(self.image_dir, keep)
        if removed:
            logger.debug(f"{TAG} 清理旧图 {removed} 张")
        return path

    async def make_post_image(self, intent: str) -> Path | None:
        """给空间说说配一张图。intent 是想表达的画面。"""
        if not bool(self._cfg("qzone_attach", True)):
            return None
        prompt = (
            "dafeiyu, 1girl, solo, cute, youthful, petite, big eyes, "
            f"{intent}, masterpiece, best quality, very aesthetic, absurdres"
        )
        try:
            return await self.make_image(prompt, tag="qzone")
        except comfy.ComfyError as exc:
            logger.warning(f"{TAG} 空间配图失败：{exc}")
            return None

    # ------------------------------------------------------------- 表情包抑制
    @filter.on_decorating_result(priority=60)
    async def strip_sticker(self, event: AstrMessageEvent):
        if not bool(self._cfg("suppress_sticker", True)):
            return
        if not bool(self._cfg("auto_enabled", False)):
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo or not self.state.has_flag(umo, "sent", ttl=90):
            return
        result = event.get_result()
        if not result or not getattr(result, "chain", None):
            return
        ours = self._our_images.get(umo, set())
        kept = []
        dropped = 0
        for comp in result.chain:
            if isinstance(comp, Image):
                name = ""
                try:
                    name = Path(str(getattr(comp, "file", "") or getattr(comp, "url", ""))).name
                except Exception:
                    name = ""
                if name in ours or not name:
                    kept.append(comp)
                    continue
                dropped += 1
                continue
            kept.append(comp)
        if dropped:
            result.chain = kept
            logger.debug(f"{TAG} 去掉了 {dropped} 个表情包")

    # ------------------------------------------------------------- 工具
    @filter.llm_tool(name="selfie_post_image")
    async def tool_post_image(self, event: AstrMessageEvent, intent: str) -> str:
        """发空间说说时，需要配一张自己的照片就用这个。

        Args:
            intent(string): 这张图想表达的画面，比如「在窗边吃早饭，晨光」「睡前抱膝坐着，台灯」
        """
        path = await self.make_post_image(intent)
        if not path:
            return "配图失败，这次说说只发文字。"
        return f"配图已生成：{path}"

    # ------------------------------------------------------------- 指令
    @filter.command("自拍")
    async def cmd_selfie(self, event: AstrMessageEvent):
        """让她现在拍一张。"""
        if not bool(self._cfg("enabled", True)):
            yield event.plain_result("自拍功能关着。")
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        raw = getattr(event, "message_str", "") or ""
        requirement = raw.replace("/自拍", "").replace("自拍", "").strip()
        verdict = await self._judge(event, umo, owner_direct=True, requirement=requirement)
        prompt = (verdict or {}).get("prompt") or ""
        if not prompt:
            yield event.plain_result("这会儿没想好拍什么…")
            return
        if self.state.has_flag(umo, "busy", ttl=240):
            yield event.plain_result("正在拍呢，等这张出来。")
            return

        yield event.plain_result("等我一下，拍一张。")
        async with self._lock(umo):
            if self.state.has_flag(umo, "busy", ttl=240):
                yield event.plain_result("正在拍呢，等这张出来。")
                return
            self.state.set_flag(umo, "busy")
            try:
                outfit_words = ("穿", "服", "裙", "衣", "装")
                strength = 0.65 if any(w in requirement for w in outfit_words) else 0.0
                path = await self.make_image(prompt, tag="cmd", strength=strength)
            except comfy.ComfyError as exc:
                yield event.plain_result(f"拍失败了：{exc}")
                return
            except Exception as exc:
                logger.warning(f"{TAG} 出图异常：{exc}")
                yield event.plain_result(f"拍失败了：{type(exc).__name__} {exc}")
                return
            finally:
                self.state.clear_flag(umo, "busy")
        self.state.mark(doing="主人在要照片", path=str(path))
        self.state.set_flag(umo, "sent")
        self._our_images.setdefault(umo, set()).add(str(path.name))
        yield event.image_result(str(path))

    @filter.command("自拍状态")
    async def cmd_status(self, event: AstrMessageEvent):
        stats = self.state.stats()
        last = time.strftime("%m-%d %H:%M", time.localtime(stats["last_at"])) if stats["last_at"] else "还没发过"
        cooldown = int(self._cfg("cooldown_minutes", DEFAULT_COOLDOWN) or 0)
        cooling = "冷却中" if self.state.in_cooldown(cooldown) else "可以发"
        group = "开" if bool(self._cfg("group_enabled", False)) else "关"
        yield event.plain_result(
            "【自拍状态】\n"
            f"档位：{self._cfg('lora_step', 12)}　权重：{self._cfg('lora_strength', 0.7)}\n"
            f"群聊生图：{group}　上一次：{last}\n"
            f"累计：{stats['total']} 张　当前：{cooling}\n"
            f"上次在干嘛：{stats['last_doing'] or '—'}"
        )

    @filter.command("自拍帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "【自拍插件】\n"
            "/自拍　　　让她现在拍一张\n"
            "/自拍状态　看档位、权重、冷却、累计张数\n"
            "/自拍帮助　这条\n\n"
            "平时不用指令：聊到有意思的画面，她自己会决定拍一张。"
        )

    # ------------------------------------------------------------- 通知
    async def _notify_owner(self, event: AstrMessageEvent, path: Path, doing: str) -> None:
        owners = self._owner_ids()
        if not owners:
            return
        try:
            for qq in owners:
                await event.bot.api.call_action(
                    "send_private_msg",
                    user_id=int(qq),
                    message=[{"type": "text", "data": {"text": f"刚拍了一张：{doing}"}},
                             {"type": "image", "data": {"file": str(path)}}],
                )
        except Exception as exc:
            logger.debug(f"{TAG} 通知主人失败：{exc}")
