"""astrbot_plugin_selfie —— 大肥鱼自己决定发不发照片。

日常聊天里她自己判断有没有画面感、想不想发一张；要发就自己写提示词，
交给本地 ComfyUI 出图，然后把图发出来。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Image, Plain, Video
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
    def _raw_text(self, event) -> str:
        """把消息里所有纯文本拼起来；拿不到就退回 message_str。"""
        try:
            parts = []
            for comp in (getattr(getattr(event, "message_obj", None), "message", None) or []):
                if isinstance(comp, Plain):
                    parts.append(str(getattr(comp, "text", "") or ""))
            text = "".join(parts).strip()
            if text:
                return text
        except Exception:
            pass
        return str(getattr(event, "message_str", "") or "").strip()

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

    # 中文俗称 → danbooru 标准 tag（主人说土话也能出对图）
    SLANG_TAGS = (
        ("假鸡巴", "dildo"), ("假阳具", "dildo"), ("假阴茎", "dildo"),
        ("玩具", "sex toy"), ("肉棒", "dildo"), ("按摩棒", "vibrator"),
        ("小穴", "pussy"), ("阴部", "pussy"),
        ("阴蒂", "clitoris"), ("乳头", "nipples"), ("乳首", "nipples"),
        ("自慰", "masturbation"), ("撸", "masturbation"), ("抠", "fingering"), ("揉", "fingering"), ("手指", "fingers"), ("用手", "hand between own legs"),
        ("插入", "penetration"), ("插进", "penetration in pussy"),
        ("抽插", "sex"), ("做爱", "sex"), ("性交", "sex"),
        ("张开腿", "spread legs"), ("掰开", "spreading"), ("露裆", "crotchless"),
        ("白丝", "white pantyhose"), ("黑丝", "black pantyhose"),
        ("高潮", "orgasm"), ("呻吟", "moaning"), ("潮吹", "squirting"),
    )

    def _normalize_requirement(self, text: str) -> str:
        """把中文俗称换成英文 tag，保留原句一起喂（桥接还会再翻一次）。"""
        out = text or ""
        hits = []
        for cn, tag in self.SLANG_TAGS:
            if cn in out:
                hits.append(tag)
        if hits:
            # 去掉重复，拼在后面
            uniq = []
            for t in hits:
                if t not in uniq:
                    uniq.append(t)
            out = f"{out}, {', '.join(uniq)}"
        return out

    # ------------------------------------------------------------- 基于上一张改
    SAME_KEYS = ("一样", "同款", "上一张", "上张", "上次那样", "照之前", "类似",
                 "差不多", "这张", "还是刚才", "再来一张", "改一下", "调整",
                 "换个姿势", "换成", "改成")

    def _wants_edit_of_last(self, requirement: str) -> bool:
        if not requirement:
            return False
        return any(k in requirement for k in self.SAME_KEYS)

    @staticmethod
    def _similar(a: str, b: str) -> float:
        """粗略相似度：按字符二元组算 Jaccard。"""
        if not a or not b:
            return 0.0
        ga = {a[i:i + 2] for i in range(len(a) - 1)}
        gb = {b[i:i + 2] for i in range(len(b) - 1)}
        if not ga or not gb:
            return 0.0
        return len(ga & gb) / len(ga | gb)

    BIG_CHANGE_KEYS = ("转过身", "转过来", "转过去", "背对", "跪", "站起来", "站起",
                       "趴", "躺下", "侧躺", "张开腿", "分开腿", "抬起", "弯腰",
                       "高潮", "换姿势", "换个姿势", "翻身", "坐起", "蹲")

    def _big_change(self, text: str) -> bool:
        return any(k in (text or "") for k in self.BIG_CHANGE_KEYS)

    # ------------------------------------------------------------- 丝袜颜色
    BLACK_HOSE = ("黑丝", "黑色丝袜", "黑丝袜", "black pantyhose", "black tights")
    WHITE_HOSE = ("白丝", "白色丝袜", "白丝袜", "white pantyhose", "white tights")

    def _apply_hose_color(self, requirement: str, prompt: str) -> tuple[str, str]:
        """主人点名黑丝/白丝就锁死颜色，另一边进负面。"""
        low = (requirement or "").lower()
        if any(k in low for k in self.BLACK_HOSE):
            if "black pantyhose" not in prompt.lower():
                prompt = f"{prompt}, black pantyhose, black tights"
            logger.info(f"{TAG} 主人要黑丝 → 锁黑丝")
            return prompt, "white pantyhose, white tights, white stockings"
        if any(k in low for k in self.WHITE_HOSE):
            if "white pantyhose" not in prompt.lower():
                prompt = f"{prompt}, white pantyhose, white tights"
            logger.info(f"{TAG} 主人要白丝 → 锁白丝")
            return prompt, "black pantyhose, black tights, black stockings"
        return prompt, ""

    # ------------------------------------------------------------- 场景提取
    SCENE_MAP = (
        (("睡觉", "睡了", "晚安", "困", "睡觉了", "躺下"), 
         "sleeping in bed, under blanket, pajamas, sleepy eyes, dim lamp light, night"),
        (("起床", "早上好", "早安", "刚醒"), 
         "just woke up, messy hair, morning sunlight through window, sitting on bed"),
        (("下雨", "雨天", "淋雨", "雷"), 
         "rain outside window, overcast light, watching the rain"),
        (("吃饭", "饿了", "吃夜宵", "吃早饭", "吃午饭", "吃晚饭"), 
         "eating at table, food on plate, holding chopsticks"),
        (("洗澡", "泡澡", "淋浴"), 
         "bathroom, wet hair, towel around body, steam"),
        (("出门", "上班", "上学", "上课", "去公司"), 
         "outdoors, walking on street, carrying bag, daylight"),
        (("海边", "沙滩", "游泳"), 
         "beach, sea waves, bright sun, summer"),
        (("看番", "追剧", "看视频", "刷手机"), 
         "sitting on sofa, watching screen, relaxed"),
    )

    def _scene_hint(self, umo: str) -> str:
        """从最近的聊天里认出她此刻在哪、在干嘛。"""
        lines = self._buffers.get(umo, [])[-10:]
        text = " ".join(lines)
        for keys, tags in self.SCENE_MAP:
            if any(k in text for k in keys):
                logger.info(f"{TAG} 场景命中：{keys[0]} → {tags[:40]}")
                return tags
        return ""

    # ------------------------------------------------------------- 自拍构图
    SELFIE_KEYS = ("自拍", "自拍照", "拍一张", "拍张照", "selfie", "合照")
    SELFIE_TAGS = ("selfie, holding smartphone, arm extended toward viewer, "
                   "phone visible in frame, from front, close-up on upper body, looking at viewer")

    @classmethod
    def _is_selfie_request(cls, text: str) -> bool:
        low = (text or "").lower()
        if any(cls._is_negated(low, k) for k in cls.SELFIE_KEYS):
            return False
        return any(k in low for k in cls.SELFIE_KEYS)

    # ------------------------------------------------------------- 否定识别
    NEG_WORDS = ("不要", "别", "不用", "不许", "不许", "禁止", "不准", "勿")

    @classmethod
    def _is_negated(cls, text: str, keyword: str) -> bool:
        """判断 text 里的 keyword 前面是不是跟着否定词。"""
        low = (text or "").lower()
        idx = low.find(keyword.lower())
        if idx < 0:
            return False
        head = low[max(0, idx - 6):idx]
        return any(w in head for w in cls.NEG_WORDS)

    # ------------------------------------------------------------- 底模路由
    # 只有「玩具 / 插入」才换底模；用手自慰继续走 Illustrious
    INSERT_KEYS = ("假鸡巴", "假阳具", "肉棒", "按摩棒", "跳蛋", "插入", "插进",
                   "抽插", "dildo", "penetration", "insert")
    FINGER_KEYS = ("自慰", "用手", "手指", "抠", "揉", "masturbat", "fingering")

    def _needs_insert_model(self, text: str) -> bool:
        low = (text or "").lower()
        return any(k in low for k in self.INSERT_KEYS)

    # ------------------------------------------------------------- 参考图保护
    FRAMING_WORDS = ("close-up on crotch", "closeup", "close-up", "cowboy shot",
                     "full body", "upper body", "from below", "from above",
                     "wide shot", "portrait", "face focus")

    @classmethod
    def _strip_framing(cls, prompt: str) -> str:
        """用参考图时必须让参考图的构图说话，提示词里的景别词要删掉。"""
        out = prompt
        for w in cls.FRAMING_WORDS:
            out = out.replace(w, "")
        return ", ".join(x.strip() for x in out.split(",") if x.strip())

    # ------------------------------------------------------------- 参考图库
    def _pick_reference(self, requirement: str):
        """从 refs 目录里挑一张跟要求最贴的参考图。"""
        ref_dir = self.data_dir / "refs"
        if not ref_dir.exists():
            return None
        meta_path = ref_dir / "refs.json"
        meta = {}
        try:
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

        best, best_score = None, 0
        low = (requirement or "").lower()
        for name, info in meta.items():
            score = sum(1 for t in info.get("tags", []) if t.lower() in low)
            if score > best_score and (ref_dir / name).exists():
                best, best_score = ref_dir / name, score
        if best:
            logger.info(f"{TAG} 用参考图：{best.name}（命中 {best_score} 个词）")
            return best

        # 没命中就轮着用，保证每次都有参考
        pool = sorted(p for p in ref_dir.glob("*.jpg")) + sorted(p for p in ref_dir.glob("*.png"))
        if pool:
            idx = int(self.state.data.get("ref_idx", 0)) % len(pool)
            self.state.data["ref_idx"] = idx + 1
            self.state.save()
            logger.info(f"{TAG} 用参考图（轮换）：{pool[idx].name}")
            return pool[idx]
        return None

    # ------------------------------------------------------------- 成人向
    # 判断成人向的关键词：只用歧义小的词（「插」「舔」「逼」这类单字会误判，已剔除）
    NSFW_KEYS = ("裸", "没穿", "全裸", "自慰", "阴蒂", "小穴", "做爱", "性交",
                 "乳头", "内射", "口交", "呻吟", "高潮", "假阳具", "假鸡巴",
                 "插入", "插进", "插入体内",
                 "nude", "naked", "pussy", "masturbat", "creampie",
                 "dildo", "uncensored")

    def _is_nsfw_request(self, text: str) -> bool:
        low = (text or "").lower()
        return any(k in low for k in self.NSFW_KEYS)

    def _nsfw_on(self) -> bool:
        return bool(self._cfg("nsfw_enabled", True))

    # ------------------------------------------------------------- 观察

    # ------------------------------------------------------------- 聊天图片
    async def _save_chat_images(self, event, umo: str) -> list[Path]:
        """把这条消息里的图片存下来，返回本地路径。"""
        saved: list[Path] = []
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        except Exception:
            chain = []
        chat_dir = self.data_dir / "chat_images"
        chat_dir.mkdir(parents=True, exist_ok=True)
        for comp in chain:
            if not isinstance(comp, Image):
                continue
            try:
                path = await comp.convert_to_file_path()
                if not path:
                    continue
                src = Path(path)
                if not src.exists():
                    continue
                stamp = int(time.time() * 1000)
                dst = chat_dir / f"chat-{stamp}.png"
                dst.write_bytes(src.read_bytes())
                saved.append(dst)
            except Exception as exc:
                logger.debug(f"{TAG} 聊天图片落盘失败：{exc}")
        if saved:
            self.state.data[f"chat_image:{umo}"] = {
                "path": str(saved[-1]),
                "ts": time.time(),
            }
            logger.info(f"{TAG} 收到聊天图片：{saved[-1].name}")
        return saved

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

        # 聊天里发的图：先落盘，后面判断/视频都能用
        imgs: list[Path] = []
        try:
            imgs = await self._save_chat_images(event, umo)
        except Exception as exc:
            logger.debug(f"{TAG} 读聊天图片出错：{exc}")

        # /视频 的追问：这里等 1/2 + 描述（必须在缓冲之前处理）
        pending_key = f"video_pending:{umo}"
        if self.state.data.get(pending_key):
            logger.info(f"{TAG} 收到视频追问回复：{text[:30]}")
            async for item in self._handle_video_answer(event, umo, text):
                yield item
            return
        if text.strip()[:1] in ("1", "2") and len(text.strip()) <= 60:
            logger.info(f"{TAG} 收到 1/2 但没有挂起的视频追问（umo={umo}）")

        # 缓冲里带上说话人，判断时才知道谁说了什么
        # 注意：这里必须在「自动判断开关」之前执行——关掉自动模式也要记聊天，
        # 否则 /自拍 看不到上下文，只能按训练集的老样子出图。
        try:
            speaker = "主人" if self._is_owner(event) else (event.get_sender_name() or "群友")
        except Exception:
            speaker = "群友"
        buf = self._buffers.setdefault(umo, [])
        if imgs:
            buf.append(f"{speaker}: [发了{len(imgs)}张图] {text[:180]}")
        else:
            buf.append(f"{speaker}: {text[:200]}")
        del buf[:-30]

        # 只用指令模式：平时聊天不自动拍图（也就不动表情包）
        if not bool(self._cfg("auto_enabled", False)):
            return

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
        last_path_hint = str(self.state.data.get("last_path", "") or "")
        last_req_hint = str(self.state.data.get("last_req", "") or "")
        is_edit = False
        if last_path_hint and Path(last_path_hint).exists():
            if self._wants_edit_of_last(requirement) or (
                last_req_hint and self._similar(requirement, last_req_hint) >= 0.5
            ):
                is_edit = True
        limit = int(self._cfg("context_lines", 16) or 16)
        lines = self._buffers.get(umo, [])[-limit:]
        context_lines = "\n".join(f"- {x}" for x in lines) or "（暂时没什么聊天内容）"

        # 上次拍过什么，让她别重复构图
        last = str(self.state.data.get("last_doing", "") or "").strip()
        if last:
            context_lines += f"\n- （上次自己拍的是：{last}，这次换个场景或姿势）"

        trigger = "主人直接要照片" if owner_direct else ""
        logger.info(f"{TAG} 判断：上下文 {len(lines)} 条，要求「{requirement[:40]}」")
        verdict = await judge.ask_should_send(
            self._provider(), context_lines, trigger, requirement,
            allow_nsfw=self._nsfw_on(), edit_hint=is_edit,
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
                         strength: float = 0.0, ref_path: "Path | None" = None,
                         denoise: float = 0.0, ckpt: str = "",
                         neg_extra: str = "") -> Path:
        """生成一张图，返回本地路径。qzone 插件也调这个。

        strength > 0 时覆盖配置里的 LoRA 权重（换服装时压低，减轻训练集惯性）。
        """
        low = prompt.lower()
        if self._nsfw_on() and any(k in low for k in ("nude", "naked", "pussy", "masturbat", "裸", "没穿", "自慰", "阴蒂")):
            anatomy = str(self._cfg("anatomy_tags", "") or "").strip()
            if anatomy:
                prompt = f"{prompt}, {anatomy}, whale tail behind body"

            # 玩具标签只在主人确实要玩具时才加
            toy_words = ("假鸡巴", "假阳具", "玩具", "肉棒", "按摩棒", "跳蛋",
                         "插入", "插进", "dildo", "vibrator", "sex toy", "penetration")
            if any(w in low for w in toy_words):
                toy = str(self._cfg("toy_tags", "") or "").strip()
                if toy:
                    prompt = f"{prompt}, {toy}"
                    logger.info(f"{TAG} 主人要玩具，追加 toy_tags")
        face = str(self._cfg("face_hint", "") or "").strip()
        if face and face.split(",")[0].strip().lower() not in prompt.lower():
            prompt = f"{prompt}, {face}"
        hint = str(self._cfg("style_hint", "") or "").strip()
        if hint:
            prompt = f"{prompt}, {hint}"
        detail = str(self._cfg("detail_tags", "") or "").strip()
        if detail:
            prompt = f"{prompt}, {detail}"
        extra = str(self._cfg("extra_tags", "") or "").strip()
        if extra:
            prompt = f"{prompt}, {extra}"
        _ref_b64 = ""
        if ref_path:
            try:
                import base64 as _b64
                # 参考图的构图优先：删掉提示词里的景别词，denoise 压到 0.45 以内
                prompt = self._strip_framing(prompt)
                denoise = min(float(denoise or 0.45), 0.45)
                _ref_b64 = _b64.b64encode(Path(ref_path).read_bytes()).decode("ascii")
                logger.info(f"{TAG} 用参考图：{Path(ref_path).name} denoise={denoise:.2f}（已去景别词）")
            except Exception as exc:
                logger.warning(f"{TAG} 读参考图失败，改回文生图：{exc}")
                _ref_b64 = ""

        raw = await asyncio.to_thread(
            comfy.generate,
            str(self._cfg("bridge_url", "http://127.0.0.1:8190")),
            prompt,
            str(self._cfg("size", "832x1152")),
            int(self._cfg("steps", 28)),
            float(self._cfg("cfg", 5.5)),
            str(self._cfg("model_name", "dafeiyu")),
            float(strength),
            _ref_b64,
            float(denoise or 0.45),
            ckpt,
            neg_extra,
        )
        path = comfy.save(raw, self.image_dir, tag=tag)

        # 成人向的另外存一份到桌面「鱼片」，方便主人自己翻
        if self._nsfw_on() and self._is_nsfw_request(prompt):
            try:
                out_dir = Path(str(self._cfg("nsfw_dir", "") or "")).expanduser()
                if str(out_dir):
                    out_dir.mkdir(parents=True, exist_ok=True)
                    import shutil as _sh
                    _sh.copy2(path, out_dir / path.name)
                    logger.info(f"{TAG} 成人向已存鱼片：{path.name}")
            except Exception as exc:
                logger.debug(f"{TAG} 存鱼片失败：{exc}")
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
        raw = self._raw_text(event)
        requirement = raw.replace("/自拍", "").replace("自拍", "").strip()
        if requirement and self._is_nsfw_request(requirement) and not self._nsfw_on():
            yield event.plain_result(str(self._cfg("nsfw_reject_text", "这个不拍啦，说点别的嘛")))
            return
        if not requirement:
            requirement = (
                "先读上面的聊天判断她此刻在干什么（睡觉？吃饭？出门？），"
                "把场景、时间、衣着、动作都写进画面；聊天里提到的地点或衣服必须出现"
            )
        verdict = await self._judge(event, umo, owner_direct=True, requirement=requirement)
        prompt = (verdict or {}).get("prompt") or ""
        if requirement:
            requirement = self._normalize_requirement(requirement)
        if not prompt and requirement:
            # 模型没给提示词（内容让她写到一半卡住时会发生）→ 直接用主人的原话兜底
            extra = str(self._cfg("anatomy_tags", "") or "").strip()
            # 把最近的聊天也拼进来，让画面接着上文
            limit = int(self._cfg("context_lines", 16) or 16)
            recent = self._buffers.get(umo, [])[-4:]
            ctx = " ".join(x.split(": ", 1)[-1] for x in recent)
            prompt = (
                "dafeiyu, 1girl, solo, deep blue hair, cyan gradient hair, fin ears, "
                "whale tail behind body, soft youthful face, detailed sparkling eyes, "
                f"{requirement}, {extra}".strip(", ")
            )
            if ctx:
                prompt = f"{prompt}, {ctx}"
            logger.info(f"{TAG} 用原话兜底出图：{requirement[:50]}")
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

                # 说得跟上一张差不多 / 明确说"和上一张一样" → 在上一张基础上改
                last_path = str(self.state.data.get("last_path", "") or "")
                last_req = str(self.state.data.get("last_req", "") or "")
                ref_path = None
                denoise = 0.0
                if last_path and Path(last_path).exists():
                    sim = self._similar(requirement, last_req)
                    if self._wants_edit_of_last(requirement) or (last_req and sim >= 0.5):
                        ref_path = Path(last_path)
                        # 幅度交给她自己判断；姿势类要求强制抬高，免得"改完跟原图一样"
                        try:
                            dval = float((verdict or {}).get("denoise") or 0)
                        except Exception:
                            dval = 0.0
                        denoise = dval if dval else float(self._cfg("edit_denoise", 0.45) or 0.45)
                        if self._big_change(requirement):
                            denoise = max(denoise, 0.58)
                            logger.info(f"{TAG} 姿势类改动，denoise 抬到 {denoise:.2f}")
                        denoise = min(0.68, max(0.28, denoise))
                        logger.info(f"{TAG} 判定为在上一张上改（相似度 {sim:.2f}）")

                # 成人向且开了「先用参考图」→ 用参考图库里的构图
                # 只有明确要玩具/插入时才用参考图库；普通的潮吹、自慰不用
                if ref_path is None and self._nsfw_on() and self._cfg("ref_use", False) \
                        and self._needs_insert_model(requirement):
                    ref_path = self._pick_reference(requirement)
                    denoise = float(self._cfg("ref_denoise", 0.55) or 0.55)

                # 从聊天里认出当前场景；但主人明确点了动作时，场景不许覆盖动作
                action_keys = self.FINGER_KEYS + self.INSERT_KEYS + (
                    "高潮", "潮吹", "裸", "没穿", "全裸", "脱", "露", "做爱", "口交",
                )
                has_action = any(k in (requirement or "").lower() for k in action_keys)
                if not has_action:
                    scene = self._scene_hint(umo)
                    if scene and scene.split(",")[0].strip().lower() not in prompt.lower():
                        prompt = f"{prompt}, {scene}"
                else:
                    logger.info(f"{TAG} 主人点了具体动作，跳过场景覆盖")
                    # 动作场景不要「举着手机自拍」那种构图
                    for w in ("selfie", "holding smartphone", "arm extended",
                              "phone visible in frame", "taking selfie"):
                        if w in prompt.lower():
                            prompt = prompt.lower().replace(w, "")
                    prompt = ", ".join(x.strip() for x in prompt.split(",") if x.strip())
                    logger.info(f"{TAG} 已去掉自拍构图词")

                # 丝袜颜色：主人说了哪个就锁哪个（neg_extra 必须先初始化）
                neg_extra = ""
                prompt, hose_neg = self._apply_hose_color(requirement, prompt)
                if hose_neg:
                    neg_extra = (neg_extra + ", " + hose_neg).strip(", ")

                # 插入类 → 换 hassaku；日常 → 用 Illustrious
                ckpt = ""
                low_req = (requirement or "").lower()
                finger_negated = any(
                    self._is_negated(low_req, k) for k in self.FINGER_KEYS
                )
                if finger_negated:
                    logger.info(f"{TAG} 主人说不要自慰 → 手放好，不加手指 tag")
                    prompt = f"{prompt}, hands on lap, arms at sides"
                    neg_extra = "hand between legs, fingering, masturbation, pussy juice"
                elif any(k in low_req for k in self.FINGER_KEYS):
                    finger = "hand between own legs, fingering, fingers inside pussy, masturbation"
                    if "fingering" not in prompt.lower():
                        prompt = f"{prompt}, {finger}"
                    logger.info(f"{TAG} 用手自慰，保持 Illustrious，补手指 tag")
                elif self._needs_insert_model(requirement) or self._needs_insert_model(prompt):
                    ckpt = str(self._cfg("insert_checkpoint", "") or "")
                    if ckpt:
                        logger.info(f"{TAG} 插入类内容，底模切到 {ckpt}")
                path = await self.make_image(prompt, tag="cmd", strength=strength,
                                             ref_path=ref_path, denoise=denoise, ckpt=ckpt,
                                             neg_extra=neg_extra)
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
        self.state.data["last_req"] = requirement
        self.state.save()
        self.state.set_flag(umo, "sent")
        self._our_images.setdefault(umo, set()).add(str(path.name))
        yield event.image_result(str(path))


    # ------------------------------------------------------------- 视频配音文本


    # ------------------------------------------------------------- 视频


    # ------------------------------------------------------------- 视频
    VIDEO_ASK = (
        "拍视频呀。要用上一张图当首帧，还是重新画一张？\n"
        "回复 1 = 用上一张（本鲸最近拍的那张）\n"
        "回复 2 = 重新画一张当首帧\n"
        "后面可以顺手写动作和环境，比如「1 躺在床上慢慢揉，卧室暖灯」"
    )

    async def make_video(self, prompt: str, first_frame: Path) -> Path | None:
        """把首帧和描述交给桥接，拿回 mp4。"""
        import base64 as _b64
        import urllib.request as _u
        url = str(self._cfg("video_url", "http://127.0.0.1:8190/v1/videos") or "")
        payload = {
            "prompt": prompt,
            "image": _b64.b64encode(first_frame.read_bytes()).decode("ascii"),
            "duration": float(self._cfg("video_duration", 5) or 5),
            "steps": int(self._cfg("video_steps", 8) or 8),
            "width": int(self._cfg("video_width", 928) or 928),
            "height": int(self._cfg("video_height", 544) or 544),
        }
        data = json.dumps(payload).encode()
        req = _u.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with _u.urlopen(req, timeout=5400) as resp:
                res = json.loads(resp.read().decode())
        except Exception as exc:
            logger.warning(f"{TAG} 视频请求失败：{exc}")
            return None
        path = res.get("path")
        if not path or not Path(path).exists():
            logger.warning(f"{TAG} 视频没有产出：{res}")
            return None
        return Path(path)

    @filter.command("视频")
    async def cmd_video(self, event: AstrMessageEvent):
        """生成视频：先问用哪张首帧。"""
        if not bool(self._cfg("video_enabled", True)):
            yield event.plain_result("视频功能关着。")
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        raw = self._raw_text(event)
        requirement = raw.replace("/视频", "").strip()
        logger.info(f"{TAG} cmd_video 收到原文：{raw[:60]} → 参数：{requirement[:50]}")

        # 支持一步到位：/视频 1 描述  或  /视频 2 描述
        head = requirement[:1]
        if head in ("1", "2"):
            rest = requirement[1:].strip(" ，,、:：")
            self.state.data[f"video_pending:{umo}"] = {
                "ts": time.time(), "requirement": rest,
            }
            logger.info(f"{TAG} 一步到位：{head} {rest[:40]}")
            async for item in self._handle_video_answer(event, umo, requirement):
                yield item
            return

        self.state.data[f"video_pending:{umo}"] = {
            "ts": time.time(),
            "requirement": requirement,
        }
        try:
            self.state.save()
        except Exception:
            pass
        logger.info(f"{TAG} 视频追问已挂起 umo={umo} 要求={requirement[:40]}")
        yield event.plain_result(self.VIDEO_ASK)

    async def _handle_video_answer(self, event: AstrMessageEvent, umo: str,
                                   text: str) -> None:
        key = f"video_pending:{umo}"
        pending = self.state.data.get(key) or {}
        if not pending:
            return
        if time.time() - float(pending.get("ts") or 0) > 300:
            self.state.data.pop(key, None)
            return

        choice = text.strip()[:1]
        if choice not in ("1", "2"):
            return
        rest = text.strip()[1:].strip(" ，,、:：")
        requirement = rest or str(pending.get("requirement") or "")
        if not requirement:
            requirement = "same camera angle, subtle movement, breathing"
        self.state.data.pop(key, None)

        # 描述交给判断模型，让它自己补全成完整的视频提示词
        try:
            verdict = await self._judge(event, umo, owner_direct=True,
                                        requirement=f"拍视频：{requirement}")
        except Exception as exc:
            logger.warning(f"{TAG} 视频提示词扩写失败，用原话：{exc}")
            verdict = {}
        video_prompt = str((verdict or {}).get("prompt") or "").strip() or requirement
        if "same camera angle" not in video_prompt.lower():
            video_prompt = f"{video_prompt}, same camera angle, steady framing, subtle motion"
        logger.info(f"{TAG} 视频提示词：{video_prompt[:120]}")

        yield event.plain_result("好，本鲸准备拍，等几分钟。")
        async with self._lock(umo):
            self.state.set_flag(umo, "busy")
            try:
                first = None
                if choice == "1":
                    chat = self.state.data.get(f"chat_image:{umo}") or {}
                    chat_path = str(chat.get("path") or "")
                    chat_ts = float(chat.get("ts") or 0)
                    last = str(self.state.data.get("last_path", "") or "")
                    last_ts = float(self.state.data.get("last_at") or 0)
                    if chat_path and Path(chat_path).exists() and chat_ts >= last_ts:
                        first = Path(chat_path)
                        logger.info(f"{TAG} 首帧用聊天里发的那张：{first.name}")
                    elif last and Path(last).exists():
                        first = Path(last)
                if first is None:
                    first = await self.make_image(video_prompt, tag="vfirst")
            except comfy.ComfyError as exc:
                self.state.clear_flag(umo, "busy")
                yield event.plain_result(f"首帧没出来：{exc}")
                return
            self.state.clear_flag(umo, "busy")

        video = await self.make_video(video_prompt, first)
        if not video:
            yield event.plain_result("视频没生成出来，看看桥接日志。")
            return
        logger.info(f"{TAG} 视频已生成：{video}")
        try:
            yield event.chain_result([Video.fromFileSystem(str(video))])
        except Exception:
            yield event.plain_result(f"视频路径：{video}")

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
