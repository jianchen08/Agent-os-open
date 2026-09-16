"""时间线游标（细化设计 §3.7，D10）：衔接链的持久载体。

cursor.json 原子更新（临时文件 + rename）；末帧抽帧由注入的
frame_extractor 端口完成（生产 = ffmpeg，测试 = 伪件），本模块只管
游标状态与承接规则的字段纪律：
- continue 连续计数达 max_consecutive（缺省 5）→ must_cut() 强制转场；
- 下一段输入快照 snapshot_for_prompt()（控 token：图鉴只带最近 N 条）。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Protocol


class FrameExtractor(Protocol):
    """末帧抽取端口：成片路径 → 末帧图片路径。"""

    async def __call__(self, video_path: str) -> str: ...


def _default_cursor() -> dict[str, Any]:
    return {
        "last_frame": "",
        "last_hook": "",
        "scene_brief": "",
        "present": [],
        "realm": "",
        "codex_count": 0,
        "codex_recent": [],
        "consecutive_continue": 0,
        "last_updated": "",
        "episode_no": 1,
        "seg_no": 0,
    }


class TimelineCursor:
    """时间线游标：文件持久 + 原子写 + 衔接纪律字段维护。"""

    def __init__(self, path: str, max_consecutive_continue: int = 5) -> None:
        self._path = Path(path)
        self._max_continue = max_consecutive_continue
        self._data = _default_cursor()
        self.load()

    # ── 持久化 ────────────────────────────────────────────────────
    def load(self) -> None:
        """读取游标文件；缺失/损坏按全新游标起步（首次开播合法态）。"""
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            merged = _default_cursor()
            merged.update(data)
            self._data = merged

    def save(self) -> None:
        """原子写：同目录临时文件 + os.replace。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), suffix=".tmp", prefix="cursor_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── 字段访问 ──────────────────────────────────────────────────
    @property
    def data(self) -> dict[str, Any]:
        """游标数据（只读视图语义，调用方不得直接改）。"""
        return dict(self._data)

    @property
    def consecutive_continue(self) -> int:
        """当前连续直续计数。"""
        return int(self._data["consecutive_continue"])

    def must_cut(self) -> bool:
        """连续直续达上限 → 下一强制转场（防末帧链画质漂移）。"""
        return self.consecutive_continue >= self._max_continue

    def has_continuable_frame(self) -> bool:
        """存在可直续的末帧。"""
        return bool(self._data["last_frame"]) and Path(str(self._data["last_frame"])).is_file()

    # ── 段落推进（W2 第⑤步调用）──────────────────────────────────
    async def advance(
        self,
        *,
        transition_type: str,
        video_path: str,
        hook: str,
        scene_brief: str,
        present: list[str],
        realm: str,
        codex_entry: dict[str, Any] | None,
        timestamp: str,
        extractor: FrameExtractor,
    ) -> dict[str, Any]:
        """段末推进游标：抽末帧 → 更新字段 → 原子落盘。

        transition_type=continue 时计数 +1，cut 清零；
        codex 条目去重（重复梗不重复入鉴）。
        """
        frame = await extractor(video_path)
        self._data["last_frame"] = frame
        self._data["last_hook"] = hook
        self._data["scene_brief"] = scene_brief
        self._data["present"] = present
        self._data["realm"] = realm
        self._data["last_updated"] = timestamp
        self._data["seg_no"] = int(self._data["seg_no"]) + 1
        if transition_type == "continue":
            self._data["consecutive_continue"] = self.consecutive_continue + 1
        else:
            self._data["consecutive_continue"] = 0
        if codex_entry:
            codex = list(self._data["codex_recent"])
            name = str(codex_entry.get("meme", ""))
            if name and name not in codex:
                codex.append(name)
                self._data["codex_recent"] = codex[-5:]
                self._data["codex_count"] = int(self._data["codex_count"]) + 1
        self.save()
        return dict(self._data)

    def snapshot_for_prompt(self) -> dict[str, Any]:
        """注入剧本生成 prompt 的快照（细化设计 §3.2 控 token 口径）。"""
        snap = dict(self._data)
        snap["must_cut"] = self.must_cut()
        return snap

    def next_transition(self, requested: str) -> str:
        """衔接仲裁：请求 continue 但（无末帧 / 计数达上限）→ 强制 cut。"""
        if requested == "continue" and (self.must_cut() or not self.has_continuable_frame()):
            return "cut"
        return requested
