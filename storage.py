"""轻量状态：消息计数、冷却、会话标记。"""

from __future__ import annotations

import json
import time
from pathlib import Path


class State:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "state.json"
        self.data = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    # ---------------------------------------------------------------- 计数
    def bump(self, umo: str) -> int:
        counters = self.data.setdefault("counters", {})
        counters[umo] = int(counters.get(umo, 0)) + 1
        return counters[umo]

    def counter(self, umo: str) -> int:
        return int(self.data.setdefault("counters", {}).get(umo, 0))

    def reset_counter(self, umo: str) -> None:
        self.data.setdefault("counters", {})[umo] = 0

    # ---------------------------------------------------------------- 冷却
    def last_at(self) -> float:
        return float(self.data.get("last_at", 0) or 0)

    def in_cooldown(self, minutes: int) -> bool:
        if minutes <= 0:
            return False
        return (time.time() - self.last_at()) < minutes * 60

    def mark(self, doing: str = "", path: str = "") -> None:
        self.data["last_at"] = time.time()
        self.data["total"] = int(self.data.get("total", 0)) + 1
        if doing:
            self.data["last_doing"] = doing
        if path:
            self.data["last_path"] = path
        self.save()

    # ---------------------------------------------------------------- 会话标记
    def set_flag(self, umo: str, name: str) -> None:
        self.data.setdefault("flags", {})[f"{umo}|{name}"] = time.time()

    def has_flag(self, umo: str, name: str, ttl: int = 120) -> bool:
        flags = self.data.setdefault("flags", {})
        ts = float(flags.get(f"{umo}|{name}", 0) or 0)
        return (time.time() - ts) < ttl

    def clear_flag(self, umo: str, name: str) -> None:
        self.data.setdefault("flags", {}).pop(f"{umo}|{name}", None)

    def stats(self) -> dict:
        return {
            "total": int(self.data.get("total", 0)),
            "last_at": self.last_at(),
            "last_doing": self.data.get("last_doing", ""),
            "counters": self.data.get("counters", {}),
        }
