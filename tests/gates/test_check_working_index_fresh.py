# @feature: FP-GATE working索引新鲜度闸(F3①) | @ci: python-coverage
"""check_working_index_fresh.py 行为契约回归锁（F3①：INDEX 提交瞬间即滞后缺口）。

缺陷真源 = docs/working/自主审查20261103/_修复复核_F2F3F7.md F3 段：INDEX 是
快照、无自动再生成触发（活体演示 42194d443 提交前 2 分钟入库致其落地即滞后）。

行为契约（断输入→输出，不钉实现）：
- 比对面 = git 跟踪面（HEAD ∪ 暂存区，CI 干净检出同语义）；跟踪面每个 .md
  必须被 INDEX 条目覆盖且链接指向正确路径，缺条目/错链 = 红 + 指路跑生成器；
- 反例防拟合（≥2 组有区分度输入）：
  a) 他会话在飞未跟踪件的条目不执法（多会话工作区不假红，CI 视角同绿）；
  b) private/ 域件不要求条目（collect 排除语义与本闸一致）；
  c) 计数行文本漂移不执法（磁盘语义与跟踪面语义的耦合漂移，重跑收敛）；
- 红例两态有区分度：条目缺失（提交面新件未入索引）与链接错指（条目在但
  链接路径错）都红，互不为彼此的特例；
- git 路径端到端：真实 tmp git 仓（git init + add）走 --root 全链。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_working_index_fresh as cwf  # noqa: E402
import gen_working_index as gwi  # noqa: E402

pytestmark = pytest.mark.unit


def _make_tree(root: Path, files: dict[str, str]) -> Path:
    """构造 docs/working 伪树，返回 working 目录。"""
    working = root / "docs" / "working"
    for rel, text in files.items():
        p = working / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return working


def _fresh_index(working: Path) -> str:
    """用生成器本体产出新鲜 INDEX 文本（测试与被测闸同源口径的地面真值）。"""
    return gwi.render(gwi.collect(working), working)


_A = {"a.md": "# 根层文档甲\n"}
_AB = {
    "a.md": "# 根层文档甲\n",
    "sub/b.md": "# 子目录文档乙\n",
    "子目录/中文件.md": "# 中文文件名条目\n",
}


_TRACKED_AB = {
    "docs/working/a.md",
    "docs/working/sub/b.md",
    "docs/working/子目录/中文件.md",
}


class TestRedCases:
    """红例：跟踪面新件未入索引 / 条目链接错指，均红且指路生成器。"""

    def test_missing_entry_red(self, tmp_path, capsys):
        working = _make_tree(tmp_path, _AB)
        # 新鲜 INDEX 删掉 sub/b.md 条目行 = 模拟 42194d443 入库而 INDEX 未再生成的滞后态
        text = _fresh_index(working)
        stale = "\n".join(
            ln for ln in text.split("\n") if not ln.startswith("- [b.md](../sub/b.md)")
        )
        (working / "INDEX.md").write_text(stale + "\n", encoding="utf-8")
        rc = cwf.check(tmp_path, tracked=_TRACKED_AB)
        out = capsys.readouterr().out
        assert rc == 1, "跟踪面件缺 INDEX 条目必须红"
        assert "gen_working_index.py" in out, "红输出必须指路生成器"
        assert "sub/b.md" in out, "diff 摘要必须点名缺失件"

    def test_mislinked_entry_red(self, tmp_path, capsys):
        working = _make_tree(tmp_path, _AB)
        # 条目存在但链接错指（写成根层链接）= 覆盖假象，必须红（与缺失互不为特例）
        text = _fresh_index(working)
        wrong = text.replace(
            "- [b.md](../sub/b.md) — 子目录文档乙", "- [b.md](b.md) — 子目录文档乙"
        )
        assert wrong != text, "测试前置：错链替换必须生效"
        (working / "INDEX.md").write_text(wrong, encoding="utf-8")
        rc = cwf.check(tmp_path, tracked=_TRACKED_AB)
        assert rc == 1, "条目链接错指必须红（覆盖假象不是覆盖）"

    def test_index_file_missing_red(self, tmp_path, capsys):
        _make_tree(tmp_path, _A)
        rc = cwf.check(tmp_path, tracked={"docs/working/a.md"})
        out = capsys.readouterr().out
        assert rc == 1, "INDEX.md 不存在 = 滞后退化态，必须红"
        assert "gen_working_index.py" in out


class TestGreenCases:
    """绿例：新鲜 INDEX / 在飞未跟踪件条目 / 计数行漂移，均不执法。"""

    def test_fresh_index_green(self, tmp_path):
        working = _make_tree(tmp_path, _AB)
        (working / "INDEX.md").write_text(_fresh_index(working), encoding="utf-8")
        assert cwf.check(tmp_path, tracked=_TRACKED_AB) == 0

    def test_foreign_untracked_entry_not_red(self, tmp_path, capsys):
        # 多会话核心场景：他会话在飞未跟踪件已在 INDEX 有条目（磁盘语义生成），
        # 跟踪面视角必须绿——CI 干净检出（无此件）同语义不假红。
        working = _make_tree(tmp_path, _AB)
        (working / "u.md").write_text("# 他在飞件\n", encoding="utf-8")
        (working / "INDEX.md").write_text(_fresh_index(working), encoding="utf-8")
        rc = cwf.check(tmp_path, tracked=_TRACKED_AB)
        out = capsys.readouterr().out
        assert rc == 0, "未跟踪件条目不得执法（多会话/CI 双视角稳定）"
        assert "u.md" in out, "未跟踪件必须显性点名留痕（禁静默）"

    def test_private_tracked_not_required(self, tmp_path):
        # collect 排除 private/ 域：private 件（即便被跟踪）不要求条目
        working = _make_tree(tmp_path, {**_A, "private/x.md": "# 私密件\n"})
        (working / "INDEX.md").write_text(_fresh_index(working), encoding="utf-8")
        tracked = {"docs/working/a.md", "docs/working/private/x.md"}
        assert cwf.check(tmp_path, tracked=tracked) == 0

    def test_count_line_drift_not_red(self, tmp_path):
        # 计数行属磁盘语义耦合漂移面（含未跟踪件计数），不属本闸执法面
        working = _make_tree(tmp_path, _A)
        text = _fresh_index(working).replace("共 1 篇", "共 999 篇")
        (working / "INDEX.md").write_text(text, encoding="utf-8")
        assert cwf.check(tmp_path, tracked={"docs/working/a.md"}) == 0


class TestGitPath:
    """端到端：真实 tmp git 仓走 --root 全链（tracked=None 的 git ls-files 路径）。"""

    def _git(self, *args: str, cwd: Path) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

    def test_git_repo_end_to_end(self, tmp_path):
        working = _make_tree(tmp_path, _AB)
        # 先落 INDEX 占位件再产出新鲜文本：与真实再生成态同构（INDEX 存在于
        # collect 时刻 → 产出含自引用条目；本闸执法面=路径覆盖，标题不比）。
        (working / "INDEX.md").write_text("占位\n", encoding="utf-8")
        self._git("init", cwd=tmp_path)
        self._git("add", "docs/working", cwd=tmp_path)
        (working / "INDEX.md").write_text(_fresh_index(working), encoding="utf-8")
        self._git("add", "docs/working/INDEX.md", cwd=tmp_path)
        assert cwf.check(tmp_path) == 0, "git 跟踪面（含暂存区）全覆盖 → 绿"

        # 同仓造滞后：新跟踪件未入 INDEX → 红
        (working / "late.md").write_text("# 迟到件\n", encoding="utf-8")
        self._git("add", "docs/working/late.md", cwd=tmp_path)
        assert cwf.check(tmp_path) == 1, "暂存区新件（未提交）缺条目即红 = F3① 缺口本体"
