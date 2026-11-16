# @feature: FP-0.2.移动端适配 Phase1 依赖审计 | @ci: python-coverage
"""audit_plugin_deps.py 单测：wheel 分类 / marker 平台排除 / 门禁合成。

行为契约（断输入→输出）：
- 分类只认锁内 wheel 平台标签（aarch64×manylinux/musllinux = 手机可装）；
- 全边被 Linux 外 marker 排除的包不入分类（win32 专属依赖不误伤门禁）；
- 门禁只收 cext-blocked 插件（fail-open 默认：无证据不降级）；
- 端到端：合成插件树跑 main → gates yaml 可被 capability_gates 解析器装载。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
GATES_DIR = Path(__file__).resolve().parents[1] / "plugins" / "shared" / "capability_gates"

pytestmark = pytest.mark.unit


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_module("audit_plugin_deps", SCRIPTS / "audit_plugin_deps.py")


class TestClassifyWheels:
    @pytest.mark.parametrize(
        ("wheels", "expected"),
        [
            (
                [
                    "https://x/pkg-1.0-cp311-cp311-manylinux_2_17_aarch64.whl",
                    "https://x/pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
                ],
                "cext-ok",
            ),
            (
                ["https://x/pkg-1.0-cp311-cp311-musllinux_1_2_aarch64.whl"],
                "cext-ok",
            ),
            (["https://x/pkg-1.0-py3-none-any.whl"], "pure"),
            # none-any 与加速轮并存：手机装纯轮，按 pure。
            (
                [
                    "https://x/pkg-1.0-py3-none-any.whl",
                    "https://x/pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl",
                ],
                "pure",
            ),
            # 仅 x86_64 linux 轮（无 aarch64）→ 手机不可装。
            (
                ["https://x/pkg-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"],
                "cext-blocked",
            ),
            # 仅 win/mac 轮 → 手机不可装。
            (
                [
                    "https://x/pkg-1.0-cp311-cp311-win_amd64.whl",
                    "https://x/pkg-1.0-cp311-cp311-macosx_11_0_arm64.whl",
                ],
                "cext-blocked",
            ),
        ],
        ids=[
            "manylinux-aarch64",
            "musllinux-aarch64",
            "pure-any",
            "mixed-any-and-accel",
            "x86-linux-only",
            "win-mac-only",
        ],
    )
    def test_classification_by_wheel_tags(self, wheels: list[str], expected: str) -> None:
        assert mod.classify_wheels(wheels) == expected

    def test_no_wheels_is_sdist_only(self) -> None:
        """零 wheel（仅 sdist）= 分类未知，不冒充任何可装结论。"""
        assert mod.classify_wheels([]) == "sdist-only"


class TestMarkerFilter:
    def test_win32_only_edge_excluded_on_phone_env(self) -> None:
        """引用边全为 win32 marker → 手机（linux aarch64）永不安装。"""
        installable, broken = mod.installable_on_phone("pywin32", ["sys_platform == 'win32'"])
        assert installable is False
        assert broken is None

    def test_unmarked_edge_installs(self) -> None:
        installable, broken = mod.installable_on_phone("anyio", [None])
        assert installable is True
        assert broken is None

    def test_aarch64_marker_installs(self) -> None:
        installable, _ = mod.installable_on_phone("pkg", ["platform_machine == 'aarch64'"])
        assert installable is True

    def test_invalid_marker_is_conservative_installable_with_evidence(self) -> None:
        """marker 畸形 = 无法证明排除手机：保守按可装参与分类，原文留痕不静默。"""
        installable, broken = mod.installable_on_phone("pkg", ["this is not a marker"])
        assert installable is True
        assert broken is not None
        assert "not a marker" in broken


class TestBuildGates:
    def test_only_blocked_plugins_gated(self) -> None:
        audits = [
            mod.PluginAudit("pure_p", "shared/pure_p", True, True, pure=["a"]),
            mod.PluginAudit("ok_p", "shared/ok_p", True, True, pure=["a"], cext_ok=["pydantic-core"]),
            mod.PluginAudit(
                "bad_p",
                "shared/bad_p",
                True,
                True,
                pure=["a"],
                cext_blocked=["winonly-c"],
            ),
        ]

        gates = mod.build_gates(audits)

        assert set(gates) == {"bad_p"}
        gate = gates["bad_p"]
        assert gate["min_profile"] == "desktop"
        assert isinstance(gate["degrade_reason"], str)
        assert "winonly-c" in gate["degrade_reason"]


class TestMarkerExclusionInLock:
    @staticmethod
    def _write_plugin(
        root: Path,
        plugin_id: str,
        lock_packages: list[dict],
    ) -> Path:
        plugin_dir = root / "shared" / plugin_id
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text(json.dumps({"id": plugin_id}), encoding="utf-8")
        (plugin_dir / "pyproject.toml").write_text(
            f'[project]\nname = "agentos-plugin-{plugin_id}"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
        lines: list[str] = []
        for pkg in lock_packages:
            lines.append("[[package]]")
            lines.append(f'name = "{pkg["name"]}"')
            lines.append(f'version = "{pkg.get("version", "1.0.0")}"')
            if "source" in pkg:
                lines.append(f"source = {pkg['source']}")
            if "dependencies" in pkg:
                deps = ", ".join(
                    f'{{ name = "{d["name"]}", marker = "{d["marker"]}" }}'
                    if d.get("marker")
                    else f'{{ name = "{d["name"]}" }}'
                    for d in pkg["dependencies"]
                )
                lines.append(f"dependencies = [{deps}]")
            if "wheels" in pkg:
                lines.append("wheels = [")
                lines.extend(f'    {{ url = "{u}" }},' for u in pkg["wheels"])
                lines.append("]")
            lines.append("")
        (plugin_dir / "uv.lock").write_text("\n".join(lines), encoding="utf-8")
        return plugin_dir

    def test_win_marked_package_does_not_gate(self, tmp_path: Path) -> None:
        """win32 专属依赖（marker 排除）不触发门禁；同锁 linux 可装包正常分类。"""
        self._write_plugin(
            tmp_path,
            "demo",
            [
                {
                    "name": "demo",
                    "source": "{ editable = { virtual = '.' } }",
                    "dependencies": [
                        {"name": "winonly", "marker": "sys_platform == 'win32'"},
                        {"name": "linuxcext", "marker": None},
                    ],
                },
                {
                    "name": "winonly",
                    "wheels": ["https://x/winonly-1.0-cp311-cp311-win_amd64.whl"],
                },
                {
                    "name": "linuxcext",
                    "wheels": ["https://x/linuxcext-1.0-cp311-cp311-manylinux_2_17_aarch64.whl"],
                },
            ],
        )

        broken: dict[str, str] = {}
        audit = mod.audit_plugin_dir(tmp_path / "shared" / "demo", tmp_path, broken)

        assert audit.plugin_id == "demo"
        assert audit.cext_blocked == []
        assert audit.cext_ok == ["linuxcext"]
        assert mod.build_gates([audit]) == {}

    def test_blocked_package_gates_with_plugin_json_id(self, tmp_path: Path) -> None:
        """linux 可装边引用的无 aarch64 轮包 → 门禁命中，键为 plugin.json id。"""
        self._write_plugin(
            tmp_path,
            "demo",
            [
                {
                    "name": "demo",
                    "source": "{ editable = { virtual = '.' } }",
                    "dependencies": [{"name": "x86only", "marker": None}],
                },
                {
                    "name": "x86only",
                    "wheels": ["https://x/x86only-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"],
                },
            ],
        )

        broken: dict[str, str] = {}
        audit = mod.audit_plugin_dir(tmp_path / "shared" / "demo", tmp_path, broken)

        gates = mod.build_gates([audit])
        assert set(gates) == {"demo"}
        assert gates["demo"]["min_profile"] == "desktop"

    def test_malformed_lock_fails_closed(self, tmp_path: Path) -> None:
        """锁文件不可解析 = fail-fast（AuditError），不产静默空审计。"""
        plugin_dir = self._write_plugin(tmp_path, "demo", [])
        (plugin_dir / "uv.lock").write_text("[[package]]\nname = ", encoding="utf-8")

        with pytest.raises(mod.AuditError, match="不可解析"):
            mod.audit_plugin_dir(plugin_dir, tmp_path, {})


class TestEndToEnd:
    def test_main_writes_parser_loadable_yaml(self, tmp_path: Path) -> None:
        """main 全链：合成树 → gates yaml 落盘 → capability_gates 解析器可装载。"""
        TestMarkerExclusionInLock._write_plugin(
            tmp_path,
            "demo",
            [
                {
                    "name": "demo",
                    "source": "{ editable = { virtual = '.' } }",
                    "dependencies": [{"name": "x86only", "marker": None}],
                },
                {
                    "name": "x86only",
                    "wheels": ["https://x/x86only-1.0-cp311-cp311-manylinux_2_17_x86_64.whl"],
                },
            ],
        )
        out = tmp_path / "capability_gates.yaml"

        rc = mod.main(["--plugins-root", str(tmp_path), "--out", str(out)])

        assert rc == 0
        sys.path.insert(0, str(GATES_DIR))
        try:
            from gates import load_capability_gates

            gates = load_capability_gates(out)
        finally:
            sys.path.remove(str(GATES_DIR))
        assert set(gates) == {"demo"}
        assert gates["demo"]["min_profile"] == "desktop"
