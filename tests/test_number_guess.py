"""猜数字游戏核心逻辑测试

覆盖所有 AC 和边界场景：
- 三个难度配置（AC-1）
- 比较提示逻辑（AC-2）
- 成绩保存/加载（AC-3）
- 排行榜前 5 升序（AC-4）
- 限定次数与状态机（AC-5）
- 边界：空排行榜、非法输入、不支持难度
"""

import json
import pytest

from scripts.number_guess import (
    DIFFICULTIES,
    Difficulty,
    GameStatus,
    Guess,
    Leaderboard,
    NumberGuessGame,
    compare,
)


# ============================================================
# AC-1: 难度配置
# ============================================================


def test_difficulty_levels_match_spec() -> None:
    """三档难度必须严格符合规格：范围 + 限定次数。"""
    assert DIFFICULTIES["easy"].min_num == 1
    assert DIFFICULTIES["easy"].max_num == 50
    assert DIFFICULTIES["easy"].max_attempts == 10

    assert DIFFICULTIES["normal"].min_num == 1
    assert DIFFICULTIES["normal"].max_num == 100
    assert DIFFICULTIES["normal"].max_attempts == 8

    assert DIFFICULTIES["hard"].min_num == 1
    assert DIFFICULTIES["hard"].max_num == 500
    assert DIFFICULTIES["hard"].max_attempts == 6


def test_difficulty_levels_complete() -> None:
    """必须恰好提供三档难度，缺一不可。"""
    assert set(DIFFICULTIES.keys()) == {"easy", "normal", "hard"}


# ============================================================
# AC-2: 大了/小了/正确 比较逻辑
# ============================================================


def test_compare_hint_higher() -> None:
    """猜测 > 答案 → 大了。"""
    assert compare(secret=50, guess=80) is Guess.HIGHER


def test_compare_hint_lower() -> None:
    """猜测 < 答案 → 小了。"""
    assert compare(secret=50, guess=20) is Guess.LOWER


def test_compare_hint_correct() -> None:
    """猜测 == 答案 → 正确。"""
    assert compare(secret=42, guess=42) is Guess.CORRECT


# ============================================================
# AC-3: 成绩持久化
# ============================================================


def test_leaderboard_save_and_load_round_trip(tmp_path) -> None:
    """写入的成绩能原样读回。"""
    path = tmp_path / "scores.json"
    lb = Leaderboard(path=path)
    lb.add(difficulty="easy", attempts=3, won=True)
    lb.add(difficulty="hard", attempts=6, won=True)

    # 重新构造实例以验证落盘真实可读
    reloaded = Leaderboard(path=path)
    assert len(reloaded.records) == 2
    assert reloaded.records[0]["difficulty"] == "easy"
    assert reloaded.records[0]["attempts"] == 3


def test_leaderboard_load_missing_file(tmp_path) -> None:
    """文件不存在时不能崩溃，应从空开始。"""
    path = tmp_path / "nope.json"
    lb = Leaderboard(path=path)
    assert lb.records == []
    assert lb.top(5) == []


def test_leaderboard_load_corrupted_file(tmp_path) -> None:
    """文件损坏时不能崩溃，应从空开始。"""
    path = tmp_path / "bad.json"
    path.write_text("{ this is not valid json")
    lb = Leaderboard(path=path)
    assert lb.records == []


def test_leaderboard_persists_to_disk(tmp_path) -> None:
    """落盘内容是合法 JSON 且字段完整。"""
    path = tmp_path / "scores.json"
    lb = Leaderboard(path=path)
    lb.add(difficulty="normal", attempts=5, won=True)

    raw = json.loads(path.read_text())
    assert isinstance(raw, list)
    assert len(raw) == 1
    assert raw[0]["difficulty"] == "normal"
    assert raw[0]["attempts"] == 5
    assert raw[0]["won"] is True
    assert "timestamp" in raw[0]


# ============================================================
# AC-4: 排行榜前 5
# ============================================================


def test_leaderboard_top5_only_returns_five(tmp_path) -> None:
    """超过 5 条记录时只返回前 5 名。"""
    lb = Leaderboard(path=tmp_path / "scores.json")
    for attempts in range(10, 0, -1):  # 10..1
        lb.add(difficulty="easy", attempts=attempts, won=True)
    assert len(lb.top(5)) == 5


def test_leaderboard_top5_sorted_ascending(tmp_path) -> None:
    """排行榜按猜测次数升序排列（少者在前）。"""
    lb = Leaderboard(path=tmp_path / "scores.json")
    for attempts in [7, 2, 5, 1, 9, 3, 4, 6, 8]:
        lb.add(difficulty="normal", attempts=attempts, won=True)
    top = lb.top(5)
    assert [r["attempts"] for r in top] == [1, 2, 3, 4, 5]


def test_leaderboard_top5_skips_lost_games(tmp_path) -> None:
    """未获胜（won=False）的记录不计入排行榜。"""
    lb = Leaderboard(path=tmp_path / "scores.json")
    lb.add(difficulty="easy", attempts=1, won=False)
    lb.add(difficulty="easy", attempts=3, won=True)
    lb.add(difficulty="easy", attempts=2, won=True)
    top = lb.top(5)
    assert len(top) == 2
    assert all(r["won"] is True for r in top)
    assert [r["attempts"] for r in top] == [2, 3]


def test_leaderboard_top_n_less_than_five(tmp_path) -> None:
    """不足 5 条时返回全部。"""
    lb = Leaderboard(path=tmp_path / "scores.json")
    lb.add(difficulty="easy", attempts=3, won=True)
    lb.add(difficulty="easy", attempts=5, won=True)
    top = lb.top(5)
    assert len(top) == 2
    assert [r["attempts"] for r in top] == [3, 5]


def test_leaderboard_empty(tmp_path) -> None:
    """无成绩时排行榜为空列表。"""
    lb = Leaderboard(path=tmp_path / "scores.json")
    assert lb.top(5) == []


# ============================================================
# AC-5: 游戏状态机 + 限定次数
# ============================================================


def test_game_starts_in_playing_state() -> None:
    """新游戏初始状态必须是 PLAYING。"""
    game = NumberGuessGame(difficulty="easy", secret=42)
    assert game.status is GameStatus.PLAYING
    assert game.attempts == 0


def test_game_won_transitions_to_won() -> None:
    """猜中后状态变为 WON，attempts 累加。"""
    game = NumberGuessGame(difficulty="easy", secret=42)
    result = game.play_round(20)
    assert result is Guess.LOWER
    assert game.status is GameStatus.PLAYING
    assert game.attempts == 1

    result = game.play_round(42)
    assert result is Guess.CORRECT
    assert game.status is GameStatus.WON
    assert game.attempts == 2


def test_game_lost_after_exhausting_attempts() -> None:
    """简单模式用完 10 次仍未猜中 → LOST。"""
    game = NumberGuessGame(difficulty="easy", secret=42)
    # 固定 10 次错误猜测（42 不在序列中）
    for i in range(10):
        guess = 1 if i % 2 == 0 else 50
        if guess == 42:
            guess = 43
        game.play_round(guess)
    assert game.status is GameStatus.LOST
    assert game.attempts == 10


def test_game_normal_lost_after_eight_attempts() -> None:
    """普通模式用完 8 次仍未猜中 → LOT。"""
    game = NumberGuessGame(difficulty="normal", secret=42)
    guesses = [10, 20, 30, 40, 50, 60, 70, 80]
    for g in guesses:
        game.play_round(g)
    assert game.status is GameStatus.LOST
    assert game.attempts == 8


def test_game_hard_lost_after_six_attempts() -> None:
    """困难模式用完 6 次仍未猜中 → LOST。"""
    game = NumberGuessGame(difficulty="hard", secret=42)
    guesses = [100, 200, 300, 400, 500, 50]
    for g in guesses:
        game.play_round(g)
    assert game.status is GameStatus.LOST
    assert game.attempts == 6


def test_game_rejects_guess_after_won() -> None:
    """胜利后继续猜测应抛出 RuntimeError。"""
    game = NumberGuessGame(difficulty="easy", secret=42)
    game.play_round(42)
    with pytest.raises(RuntimeError):
        game.play_round(1)


def test_game_rejects_guess_after_lost() -> None:
    """失败后继续猜测应抛出 RuntimeError。"""
    game = NumberGuessGame(difficulty="easy", secret=42)
    # 用完 10 次
    for g in [1, 50, 2, 49, 3, 48, 4, 47, 5, 46]:
        game.play_round(g)
    assert game.status is GameStatus.LOST
    with pytest.raises(RuntimeError):
        game.play_round(42)


def test_game_rejects_unsupported_difficulty() -> None:
    """不支持的难度应抛出 ValueError。"""
    with pytest.raises(ValueError):
        NumberGuessGame(difficulty="impossible", secret=42)


def test_game_rejects_out_of_range_guess() -> None:
    """超出难度范围的猜测应抛出 ValueError。"""
    game = NumberGuessGame(difficulty="easy", secret=42)  # easy: 1..50
    with pytest.raises(ValueError):
        game.play_round(0)
    with pytest.raises(ValueError):
        game.play_round(51)


def test_game_accepts_boundary_guesses() -> None:
    """边界值（最小/最大）必须被接受。"""
    game = NumberGuessGame(difficulty="easy", secret=42)
    # 1 是合法下界
    assert game.play_round(1) is Guess.LOWER  # 1 < 42
    # 50 是合法上界
    assert game.play_round(50) is Guess.HIGHER  # 50 > 42


# ============================================================
# 边界：非数字输入（仅约束类型，不约束字符串解析，由 UI 处理）
# ============================================================


def test_compare_treats_equal_as_correct() -> None:
    """边界值等价比较。"""
    assert compare(secret=1, guess=1) is Guess.CORRECT
    assert compare(secret=1, guess=2) is Guess.HIGHER
    assert compare(secret=1, guess=0) is Guess.LOWER
