# @feature: FP-0.2.装机venv自愈 | @ci: python-coverage
"""check_plugin_venv_health 门禁测试（打包前置关键链 venv 健康闸）。

锁三条不变量：
- 在场但不健康（SDK 不可 import / 解释器缺失）→ unhealthy（fail-closed 主判据）；
- .venv 缺席 → lenient 记 absent、strict 记 unhealthy（冷检出 vs 打包前两形态）；
- iter_targets 选形语义透传：合宿成员与无 pyproject 插件不进检查面（单一真值）。

执行体一律注入假 probe（进程拉起是外部依赖，mock 正当，与
test_ensure_plugin_venvs 同纪律）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_SPEC = importlib.util.spec_from_file_location(
    "check_plugin_venv_health", _SCRIPTS / "check_plugin_venv_health.py"
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - 同仓固定路径必成功
    raise RuntimeError("scripts/check_plugin_venv_health.py 缺失")
G = importlib.util.module_from_spec(_SPEC)
sys.modules["check_plugin_venv_health"] = G
_SPEC.loader.exec_module(G)


def _make_plugin(root: Path, name: str, *, sdk: bool, venv: str | None) -> Path:
    """造插件目录：sdk=pyproject 声明 SDK；venv=None 缺席 / "healthy" / "stale"。"""
    d = root / name
    d.mkdir(parents=True)
    (d / "plugin.json").write_text('{"id": "%s"}' % name, encoding="utf-8")
    if sdk:
        (d / "pyproject.toml").write_text(
            'dependencies = ["agentos-plugin-sdk"]\n', encoding="utf-8"
        )
    if venv is not None:
        interp = G.epv.venv_interpreter_path(d)
        interp.parent.mkdir(parents=True, exist_ok=True)
        interp.write_text("interp-marker", encoding="utf-8")
        if venv == "stale":
            (d / ".venv" / "corpse.txt").write_text("stale-editable", encoding="utf-8")
    return d


def test_categorizes_healthy_stale_absent(tmp_path: Path) -> None:
    """三态归桶：健康/在场不健康/缺席各归其位（probe 按插件名区分健康度）。"""
    _make_plugin(tmp_path, "p_ok", sdk=True, venv="healthy")
    _make_plugin(tmp_path, "p_stale", sdk=True, venv="stale")
    _make_plugin(tmp_path, "p_absent", sdk=True, venv=None)

    def probe(interp: Path) -> bool:
        return "p_ok" in str(interp)

    healthy, absent, unhealthy = G.check_targets(tmp_path, import_probe=probe)

    assert healthy == ["p_ok"]
    assert len(absent) == 1 and absent[0].startswith("p_absent:")
    assert len(unhealthy) == 1 and unhealthy[0].startswith("p_stale:")
    assert "SDK 不可 import" in unhealthy[0]


def test_missing_interpreter_is_unhealthy(tmp_path: Path) -> None:
    """.venv 在场但解释器文件缺失 → unhealthy（空壳 venv 形态）。"""
    d = _make_plugin(tmp_path, "p_shell", sdk=True, venv=None)
    (d / ".venv").mkdir()

    _healthy, _absent, unhealthy = G.check_targets(tmp_path, import_probe=lambda _p: True)

    assert len(unhealthy) == 1 and "解释器缺失" in unhealthy[0]


def test_absent_becomes_fail_under_strict(tmp_path: Path) -> None:
    """strict 档：未供给也算 FAIL（打包前本地形态）。"""
    _make_plugin(tmp_path, "p_absent", sdk=True, venv=None)

    _h, absent_lenient, _u = G.check_targets(tmp_path, import_probe=lambda _p: True)
    _h2, _a2, unhealthy_strict = G.check_targets(
        tmp_path, import_probe=lambda _p: True, strict=True
    )

    assert len(absent_lenient) == 1
    assert len(unhealthy_strict) == 1 and "未供给" in unhealthy_strict[0]


def test_cohost_and_native_plugins_excluded(tmp_path: Path) -> None:
    """合宿成员（host_group）与无 pyproject 插件不进检查面（iter_targets 单一真值）。"""
    cohost = _make_plugin(tmp_path, "p_cohost", sdk=True, venv=None)
    (cohost / "plugin.json").write_text(
        '{"id": "p_cohost", "host_group": "light"}', encoding="utf-8"
    )
    _make_plugin(tmp_path, "p_native", sdk=False, venv=None)

    healthy, absent, unhealthy = G.check_targets(tmp_path, import_probe=lambda _p: False)

    assert healthy == absent == unhealthy == []


def test_real_repo_lenient_no_unhealthy() -> None:
    """真仓 lenient 冒烟：不得存在「在场但不健康」的关键链 venv。

    CI 冷检出全 absent → lenient 跳过=过；本地长寿命检出有漂移 → 此处红。
    （不注入 probe，走真子进程 import 探测——判据与生产同一路径。）
    """
    _healthy, _absent, unhealthy = G.check_targets(REPO / "plugins" / "shared")
    assert unhealthy == [], f"仓内关键链 venv 存在不健康项：{unhealthy}"
