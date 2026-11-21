# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""skill_manager 技能注册表单测（T3 注册权威）。

断 list_skills / get_skill / parse_skill_md / detect_languages 的可观察行为：
front matter 与标题推断两组解析输入、排序与坏件跳过、languages 深度检测、
空目录/缺目录、get 未找到与穿越形态；X11-F1 起另断四源并集清单（模式包
技能在列 + source 标注 + 同名快照同款优先序 + list/get 同一解析面 + 坏件
跳过带路径 warning）与 skill_source_dirs 四源解析契约。≥2 组区分输入 +
tmp_path 造仓。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SHARED_ROOT = Path(__file__).resolve().parents[1]
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from skill_manager import skill_registry as sr  # noqa: E402


def _plant_skill(
    root: Path,
    name: str,
    md: str | None = "plain",
    *,
    scripts: tuple[str, ...] = (),
    raw_md_bytes: bytes | None = None,
) -> Path:
    """造一个技能目录；md=None = 无清单文件。"""
    d = root / "skills" / name
    d.mkdir(parents=True)
    if raw_md_bytes is not None:
        (d / "SKILL.md").write_bytes(raw_md_bytes)
    elif md is not None:
        (d / "SKILL.md").write_text(md, encoding="utf-8")
    for rel in scripts:
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# script\n", encoding="utf-8")
    return d


FRONT_MATTER_MD = "---\nname: One Skill\ndescription: the first skill\n---\nbody of one\n"
TITLE_MD = "# Two Skill\n\nsecond skill paragraph\n"
BAD_FRONT_MD = "---\n: : : not yaml [\n---\nbody here\n\nfallback paragraph\n"


class TestParseSkillMd:
    def test_front_matter_name_description_body(self) -> None:
        name, description, body = sr.parse_skill_md(FRONT_MATTER_MD, "fallback")
        assert (name, description) == ("One Skill", "the first skill")
        assert body == "body of one\n"

    def test_title_and_paragraph_fallback(self) -> None:
        name, description, body = sr.parse_skill_md(TITLE_MD, "fallback")
        assert name == "Two Skill"
        assert description == "second skill paragraph"
        assert body == TITLE_MD  # 无 front matter 时正文原样

    @pytest.mark.parametrize("md", [BAD_FRONT_MD, ""])
    def test_degraded_inputs_keep_fallback_name(self, md: str) -> None:
        name, description, _ = sr.parse_skill_md(md, "fallback")
        assert name == "fallback"
        assert isinstance(description, str)

    def test_title_fallback_wins_only_without_front_name(self) -> None:
        # front matter 有 name 时标题不再覆盖；有 title 无 name 时取 title
        md = "---\ntitle: Titled\ndescription: d\n---\n# Heading\ntext\n"
        assert sr.parse_skill_md(md, "fallback")[0] == "Titled"
        md2 = "---\nname: Named\n---\n# Heading\ntext\n"
        assert sr.parse_skill_md(md2, "fallback")[0] == "Named"


class TestDetectLanguages:
    def test_depth_scripts_detected(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "scripted", scripts=("scripts/helper.py", "run.sh"))
        assert sr.detect_languages(tmp_path / "skills" / "scripted") == ["python", "bash"]

    def test_plain_dir_no_languages(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "plain")
        assert sr.detect_languages(tmp_path / "skills" / "plain") == []

    @pytest.mark.parametrize(
        ("script", "lang"),
        [("a.js", "nodejs"), ("a.ps1", "powershell"), ("deep/nest/a.py", "python")],
    )
    def test_each_ext_maps_to_language(self, tmp_path: Path, script: str, lang: str) -> None:
        _plant_skill(tmp_path, "s", scripts=(script,))
        assert sr.detect_languages(tmp_path / "skills" / "s") == [lang]

    def test_pruned_dependency_dirs_not_counted(self, tmp_path: Path) -> None:
        """依赖/构建产物重目录整枝剪掉（技能包带 node_modules/.venv 不炸扫描）。

        scripts/a.py 在 node_modules 下 → 不计入；run.sh 在顶层 → 正常计入；
        正常子目录 scripts2/helper.py 深度不受剪枝影响。"""
        d = _plant_skill(tmp_path, "heavy", scripts=("run.sh", "scripts2/helper.py"))
        dep = d / "node_modules" / "some-dep"
        dep.mkdir(parents=True)
        (dep / "impl.py").write_text("# dep\n", encoding="utf-8")
        venv = d / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "site.py").write_text("# venv\n", encoding="utf-8")
        # node_modules/.venv 内的 .py 不计入；正常子目录 .py 不受剪枝影响
        assert sr.detect_languages(d) == ["python", "bash"]

    def test_early_exit_after_all_languages_found(self, tmp_path: Path) -> None:
        """四语齐即停走树（大目录截断语义，行为面 = 返回全集）。"""
        _plant_skill(
            tmp_path,
            "quad",
            scripts=("a.py", "b.js", "c.sh", "d.ps1", "more/nested/e.py"),
        )
        assert sr.detect_languages(tmp_path / "skills" / "quad") == [
            "python",
            "nodejs",
            "bash",
            "powershell",
        ]


class TestListSkills:
    def test_sorted_metadata_without_content(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "zeta", FRONT_MATTER_MD, scripts=("x.py",))
        _plant_skill(tmp_path, "alpha", TITLE_MD)
        items = sr.list_skills(tmp_path / "skills")
        assert [it["id"] for it in items] == ["alpha", "zeta"]  # 排序稳定
        assert items[0]["name"] == "Two Skill"
        assert items[1]["description"] == "the first skill"
        assert items[1]["languages"] == ["python"]
        assert all("content" not in it for it in items)  # 列表只回元数据

    def test_skips_broken_and_mdless_dirs(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "good", FRONT_MATTER_MD)
        _plant_skill(tmp_path, "broken", raw_md_bytes=b"\xff\xfe\x00bad")
        _plant_skill(tmp_path, "mdless", md=None)
        (tmp_path / "skills" / "loose_file.txt").write_text("not a dir", encoding="utf-8")
        assert [it["id"] for it in sr.list_skills(tmp_path / "skills")] == ["good"]

    def test_empty_and_missing_dir(self, tmp_path: Path) -> None:
        (tmp_path / "skills").mkdir()
        assert sr.list_skills(tmp_path / "skills") == []
        assert sr.list_skills(tmp_path / "no_such") == []
        assert sr.list_skills(None) == sr.list_skills()  # 缺省 = 仓内真实 skills/


class TestGetSkill:
    def test_found_with_content(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "one", FRONT_MATTER_MD, scripts=("s.py",))
        out = sr.get_skill("one", tmp_path / "skills")
        assert out["found"] is True
        skill: dict[str, Any] = out["skill"]
        assert skill["name"] == "One Skill"
        assert skill["content"] == "body of one\n"
        assert skill["languages"] == ["python"]

    @pytest.mark.parametrize("skill_id", ["no_such", "", "../escape", "a/b", "x" * 65])
    def test_not_found_or_rejected_ids(self, tmp_path: Path, skill_id: str) -> None:
        _plant_skill(tmp_path, "one", FRONT_MATTER_MD)
        assert sr.get_skill(skill_id, tmp_path / "skills") == {"found": False}

    def test_broken_md_is_not_found(self, tmp_path: Path) -> None:
        _plant_skill(tmp_path, "broken", raw_md_bytes=b"\xff\xfe\x00bad")
        assert sr.get_skill("broken", tmp_path / "skills") == {"found": False}


class TestMultiSourceUnion:
    """四源并集清单（X11-F1 / P-87）：注册表与快照侧同源解析。

    断清单可见性（模式包技能在列）、source 来源标注（实际胜出源）、同名
    快照同款优先序（升优先序后源胜出）、list/get 同一解析面、坏件跳过带
    路径 warning（P-91 可见性）。源注入经 _default_sources 缝（生产缺省走
    真实四源推导，测试给封闭面）。
    """

    def test_mode_skill_visible_and_labeled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, mode = tmp_path / "repo", tmp_path / "mode_coding"
        _plant_skill(repo, "repo_only", FRONT_MATTER_MD)
        _plant_skill(mode, "mode_skill", FRONT_MATTER_MD)
        monkeypatch.setattr(
            sr,
            "_default_sources",
            lambda: [(repo / "skills", "repo_root"), (mode / "skills", "mode:mode_coding")],
        )
        items = sr.list_skills()
        assert {it["id"]: it["source"] for it in items} == {
            "repo_only": "repo_root",
            "mode_skill": "mode:mode_coding",
        }
        assert all("content" not in it for it in items)  # 清单仍只回元数据

    def test_same_name_latest_source_wins_like_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, mode, user = tmp_path / "repo", tmp_path / "mode_x", tmp_path / "user_root"
        _plant_skill(repo, "dup", "---\nname: Repo Copy\ndescription: d\n---\nrepo body\n")
        _plant_skill(mode, "dup", "---\nname: Mode Copy\ndescription: d\n---\nmode body\n")
        _plant_skill(user, "solo", FRONT_MATTER_MD)
        monkeypatch.setattr(
            sr,
            "_default_sources",
            lambda: [
                (repo / "skills", "repo_root"),
                (mode / "skills", "mode:mode_x"),
                (user / "skills", "user_root"),
            ],
        )
        by_id = {it["id"]: it for it in sr.list_skills()}
        assert by_id["dup"]["source"] == "mode:mode_x"  # 快照同款：升优先序后源胜出
        assert by_id["dup"]["name"] == "Mode Copy"
        assert by_id["solo"]["source"] == "user_root"

    def test_get_skill_same_union_same_winner(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo, mode = tmp_path / "repo", tmp_path / "mode_x"
        _plant_skill(repo, "dup", "---\nname: R\ndescription: d\n---\nrepo body\n")
        _plant_skill(mode, "dup", "---\nname: M\ndescription: d\n---\nmode body\n")
        monkeypatch.setattr(
            sr,
            "_default_sources",
            lambda: [(repo / "skills", "repo_root"), (mode / "skills", "mode:mode_x")],
        )
        out = sr.get_skill("dup")
        assert out["found"] is True
        assert out["skill"]["content"] == "mode body\n"  # 清单可见即可取，同一胜负序
        assert sr.get_skill("no_such") == {"found": False}

    def test_broken_entries_skip_with_path_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = tmp_path / "one"
        _plant_skill(root, "good", FRONT_MATTER_MD)
        _plant_skill(root, "broken", raw_md_bytes=b"\xff\xfe\x00bad")
        _plant_skill(root, "mdless", md=None)
        monkeypatch.setattr(sr, "_default_sources", lambda: [(root / "skills", "repo_root")])
        with caplog.at_level(logging.WARNING):
            items = sr.list_skills()
        assert [it["id"] for it in items] == ["good"]
        warned = [r.getMessage() for r in caplog.records if r.name.endswith("skill_registry")]
        assert len(warned) == 2  # 两类坏件各留一条（P-91：跳过必须可见）
        assert all("broken" in m or "mdless" in m for m in warned)


class TestSkillSourceDirs:
    """workspace_lifecycle.skill_source_dirs（快照/清单同源单点）的四源解析契约。"""

    @staticmethod
    def _fn() -> Any:
        from _root_api import ensure_isolation_path  # noqa: PLC0415

        ensure_isolation_path()
        from isolation.workspace_lifecycle import skill_source_dirs  # noqa: PLC0415

        return skill_source_dirs

    def test_injected_replaces_derivation(self, tmp_path: Path) -> None:
        base, inj = tmp_path / "base", tmp_path / "inj"
        base.mkdir()
        inj.mkdir()
        # 注入 = 封闭面：仅 base + 注入清单，不叠加出厂/用户推导（快照侧既有语义）
        assert self._fn()(base, injected=[inj]) == [
            (base / "skills", "repo_root"),
            (inj, "injected"),
        ]

    def test_four_sources_in_priority_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import user_space

        base = tmp_path / "repo"
        (base / "skills" / "s").mkdir(parents=True)
        user_plugins = tmp_path / "upl"
        mode_skills = user_plugins / "modes" / "mode_userpkg" / "skills"
        mode_skills.mkdir(parents=True)
        user_root = tmp_path / "uroot"
        user_root.mkdir()
        monkeypatch.setattr(user_space, "user_plugins_dir", lambda: user_plugins)
        monkeypatch.setattr(user_space, "user_root", lambda: user_root)
        sources = self._fn()(base)
        assert sources[0] == (base / "skills", "repo_root")
        assert sources[-1] == (user_root / "skills", "user_root")
        assert (mode_skills, "mode:mode_userpkg") in sources
        # 出厂包取自共享根真实布局（本仓 mode_* 实体），四种来源按升优先序排列
        import isolation.workspace_lifecycle as wl

        factory = Path(wl.__file__).resolve().parents[1] / "mode_coding" / "skills"
        assert (factory, "mode:mode_coding") in sources
        order = [label for _, label in sources]
        assert (
            order.index("repo_root")
            < order.index("mode:mode_coding")
            < order.index("mode:mode_userpkg")
            < order.index("user_root")
        )

    def test_user_space_unavailable_keeps_repo_and_factory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import user_space

        def _boom() -> Path:
            raise RuntimeError("user space unavailable")

        monkeypatch.setattr(user_space, "user_plugins_dir", _boom)
        monkeypatch.setattr(user_space, "user_root", _boom)
        base = tmp_path / "repo"
        (base / "skills").mkdir(parents=True)
        sources = self._fn()(base)
        assert sources[0] == (base / "skills", "repo_root")
        assert not any(label == "user_root" for _, label in sources)
        assert any(label == "mode:mode_coding" for _, label in sources)  # 出厂包不依赖 user_space
