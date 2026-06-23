"""猜数字小游戏

可独立运行：``python scripts/number_guess.py``
亦可作为模块导入做单元测试。

核心数据流：

    UI (stdin/stdout) -> NumberGuessGame -> Leaderboard -> JSON 文件
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


# ============================================================
# 公共类型
# ============================================================


class Guess(Enum):
    """单次猜测的比较结果。"""

    HIGHER = "high"   # 猜大了
    LOWER = "low"     # 猜小了
    CORRECT = "ok"    # 猜对了


class GameStatus(Enum):
    """游戏状态机。"""

    PLAYING = "playing"
    WON = "won"
    LOST = "lost"


@dataclass(frozen=True)
class Difficulty:
    """难度档位配置：数值范围 + 限定次数。"""

    name: str
    min_num: int
    max_num: int
    max_attempts: int


# 难度配置：与需求规格严格一致（AC-1）
DIFFICULTIES: dict[str, Difficulty] = {
    "easy":   Difficulty(name="简单", min_num=1,   max_num=50,  max_attempts=10),
    "normal": Difficulty(name="普通", min_num=1,   max_num=100, max_attempts=8),
    "hard":   Difficulty(name="困难", min_num=1,   max_num=500, max_attempts=6),
}


# ============================================================
# 核心逻辑：纯函数
# ============================================================


def compare(secret: int, guess: int) -> Guess:
    """比较猜测与答案，返回高了/低了/正确（AC-2）。"""
    if guess > secret:
        return Guess.HIGHER
    if guess < secret:
        return Guess.LOWER
    return Guess.CORRECT


# ============================================================
# 排行榜：JSON 持久化（AC-3、AC-4）
# ============================================================


@dataclass
class Leaderboard:
    """成绩持久化 + 排行榜计算。

    文件结构（list[dict]）：
        [
            {"difficulty": "easy", "attempts": 3, "won": true, "timestamp": "..."},
            ...
        ]
    """

    path: Path
    records: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 启动时尝试从磁盘加载，文件缺失/损坏均降级为空（测试覆盖此分支）
        self.records = self._load()

    def _load(self) -> list[dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        # 仅保留字段齐全且类型正确的记录，丢弃异常数据
        return [r for r in data if self._is_valid(r)]

    @staticmethod
    def _is_valid(record: Any) -> bool:
        if not isinstance(record, dict):
            return False
        return (
            isinstance(record.get("difficulty"), str)
            and isinstance(record.get("attempts"), int)
            and isinstance(record.get("won"), bool)
        )

    def add(self, difficulty: str, attempts: int, won: bool) -> None:
        """记录一条成绩并落盘。"""
        self.records.append(
            {
                "difficulty": difficulty,
                "attempts": attempts,
                "won": won,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def top(self, n: int) -> list[dict[str, Any]]:
        """返回前 N 名（按猜测次数升序，仅含获胜记录）（AC-4）。"""
        winners = [r for r in self.records if r["won"]]
        winners.sort(key=lambda r: r["attempts"])
        return winners[:n]


# ============================================================
# 游戏状态机（AC-5）
# ============================================================


class NumberGuessGame:
    """单局游戏状态机：PLAYING -> (WON | LOST)

    依赖注入 secret，便于测试。
    """

    def __init__(self, difficulty: str, secret: int) -> None:
        if difficulty not in DIFFICULTIES:
            raise ValueError(f"不支持的难度: {difficulty!r}")
        self._config = DIFFICULTIES[difficulty]
        self._difficulty_key = difficulty
        self._secret = secret
        self._attempts = 0
        self._status = GameStatus.PLAYING

    # ---------- 只读属性 ----------

    @property
    def status(self) -> GameStatus:
        return self._status

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def config(self) -> Difficulty:
        return self._config

    @property
    def secret(self) -> int:
        return self._secret

    # ---------- 主操作 ----------

    def play_round(self, guess: int) -> Guess:
        """提交一次猜测，返回比较结果。

        - 状态机：游戏结束后再次调用抛 RuntimeError
        - 边界：超出难度范围的猜测抛 ValueError
        """
        if self._status is not GameStatus.PLAYING:
            raise RuntimeError(f"游戏已结束（{self._status.value}），不能再猜测")

        cfg = self._config
        if not (cfg.min_num <= guess <= cfg.max_num):
            raise ValueError(
                f"猜测值 {guess} 超出 {cfg.name} 范围 [{cfg.min_num}, {cfg.max_num}]"
            )

        self._attempts += 1
        result = compare(self._secret, guess)

        if result is Guess.CORRECT:
            self._status = GameStatus.WON
        elif self._attempts >= cfg.max_attempts:
            self._status = GameStatus.LOST

        return result


# ============================================================
# 内部实现：UI 主循环（仅当脚本直接运行时使用）
# ============================================================


def _default_leaderboard_path() -> Path:
    """排行榜默认存储位置：脚本同目录下 scores.json。"""
    return Path(__file__).resolve().parent / "scores.json"


def _prompt_int(prompt: str) -> int:
    """读取整数输入；输入非法时递归重试。"""
    raw = input(prompt).strip()
    try:
        return int(raw)
    except ValueError:
        print(f"  ⚠ 请输入整数，收到: {raw!r}")
        return _prompt_int(prompt)


def _print_hud(game: NumberGuessGame) -> None:
    cfg = game.config
    print(
        f"  范围: {cfg.min_num} ~ {cfg.max_num}  "
        f"剩余次数: {cfg.max_attempts - game.attempts}/{cfg.max_attempts}"
    )


def _play_one_round(leaderboard: Leaderboard) -> None:
    print("\n请选择难度：")
    print("  1) 简单 (1-50, 10 次)")
    print("  2) 普通 (1-100, 8 次)")
    print("  3) 困难 (1-500, 6 次)")

    choice = input("  难度编号 [1/2/3]: ").strip()
    mapping = {"1": "easy", "2": "normal", "3": "hard"}
    if choice not in mapping:
        print("  ✗ 无效选择，已取消本局。")
        return
    difficulty = mapping[choice]
    cfg = DIFFICULTIES[difficulty]

    secret = random.randint(cfg.min_num, cfg.max_num)
    game = NumberGuessGame(difficulty=difficulty, secret=secret)

    print(f"\n=== {cfg.name}模式开始 ===")
    while game.status is GameStatus.PLAYING:
        _print_hud(game)
        guess = _prompt_int("  你的猜测: ")
        try:
            result = game.play_round(guess)
        except ValueError as e:
            print(f"  ✗ {e}")
            continue

        if result is Guess.HIGHER:
            print("  📉 大了")
        elif result is Guess.LOWER:
            print("  📈 小了")
        else:
            print(f"  🎉 正确！共猜了 {game.attempts} 次。")

    if game.status is GameStatus.LOST:
        print(f"  💔 次数用完，正确答案是 {game.secret}。")

    leaderboard.add(
        difficulty=difficulty,
        attempts=game.attempts,
        won=game.status is GameStatus.WON,
    )


def _print_leaderboard(leaderboard: Leaderboard) -> None:
    print("\n=== 排行榜 TOP 5 ===")
    top = leaderboard.top(5)
    if not top:
        print("  暂无成绩。")
        return
    for i, r in enumerate(top, start=1):
        cfg = DIFFICULTIES.get(r["difficulty"])
        diff_name = cfg.name if cfg else r["difficulty"]
        print(f"  {i}. {diff_name}  猜了 {r['attempts']} 次")


def main() -> int:
    print("=" * 40)
    print("       猜数字小游戏")
    print("=" * 40)
    leaderboard = Leaderboard(path=_default_leaderboard_path())
    while True:
        print("\n主菜单：")
        print("  1) 开始一局")
        print("  2) 查看排行榜")
        print("  3) 退出")
        choice = input("  请选择 [1/2/3]: ").strip()
        if choice == "1":
            _play_one_round(leaderboard)
        elif choice == "2":
            _print_leaderboard(leaderboard)
        elif choice == "3":
            print("  再见！")
            return 0
        else:
            print("  ✗ 无效选择，请重新输入。")


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DIFFICULTIES",
    "Difficulty",
    "GameStatus",
    "Guess",
    "Leaderboard",
    "NumberGuessGame",
    "compare",
]
