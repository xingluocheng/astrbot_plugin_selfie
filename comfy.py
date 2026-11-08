"""调用本地 ComfyUI 桥接生成图片。"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from astrbot.api import logger


class ComfyError(RuntimeError):
    pass


def _post_json(url: str, payload: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def generate(
    bridge_url: str,
    prompt: str,
    size: str = "832x1152",
    steps: int = 28,
    cfg: float = 5.5,
    model: str = "dafeiyu",
    strength: float = 0.0,
    timeout: int = 600,
) -> bytes:
    """向桥接发一次请求，返回 PNG 字节。"""
    base = (bridge_url or "").rstrip("/")
    if not base:
        raise ComfyError("还没配生图接口地址")

    payload = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "steps": int(steps),
        "cfg": float(cfg),
    }
    if strength > 0:
        payload["lora_strength"] = float(strength)

    started = time.time()
    try:
        data = _post_json(f"{base}/v1/images/generations", payload, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ComfyError(f"接口返回 {exc.code}：{detail}") from exc
    except Exception as exc:
        raise ComfyError(f"连不上生图接口（{base}）：{exc}") from exc

    items = data.get("data") or []
    if not items:
        raise ComfyError("接口没返回图片")
    b64 = items[0].get("b64_json") or ""
    if not b64:
        url = items[0].get("url")
        if url:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return resp.read()
        raise ComfyError("接口返回里没有图片数据")

    raw = base64.b64decode(b64)
    logger.debug(f"[selfie] 出图耗时 {time.time() - started:.1f}s，{len(raw) // 1024} KB")
    return raw


def save(raw: bytes, folder: Path, tag: str = "selfie") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    path = folder / name
    path.write_bytes(raw)
    return path


def prune(folder: Path, keep: int) -> int:
    """只保留最近 keep 张，返回删掉的张数。"""
    if keep <= 0 or not folder.exists():
        return 0
    files = sorted(folder.glob("*.png"), key=lambda p: p.stat().st_mtime)
    extra = len(files) - keep
    removed = 0
    for p in files[:max(0, extra)]:
        try:
            p.unlink()
            removed += 1
        except Exception:
            pass
    return removed
