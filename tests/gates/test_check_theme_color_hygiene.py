# @feature: FP-0.2.CFG 主题色卫生门禁 | @ci: python-test
"""check_theme_color_hygiene.py 单测（2026-10-25 用户裁定：颜色一律随主题）。

行为契约（断输入→输出，不钉实现）：
- 检出面：hex / rgb()/rgba()/hsl()/hsla() 实参字面量命中；var(--token) 与
  hsl(var(--token)) 主题变量消费不命中；
- 豁免层：index.css / styles/** / themes/** / config/themes/** /
  themeService.ts / themeValuePolicy.ts / storeTestMocks.ts（测试夹具）/
  *.test.* / dsh_adapter plugin.json 不计；
- 基线棘轮：现命中 > 上限 = 红（退出码 1）；上限 = 现命中 = 绿；
  --baseline-monotonic 下上限 > 现命中或基线文件已删 = 红（不许回松）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_theme_color_hygiene", SCRIPTS / "check_theme_color_hygiene.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_theme_color_hygiene"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """隔离扫描根：REPO 指向 tmp 目录，镜像 frontend/src 结构。"""
    mod = _load()
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "plugins" / "shared" / "demo").mkdir(parents=True)
    monkeypatch.setattr(mod, "REPO", tmp_path)
    yield mod, tmp_path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_detects_hardcoded_colors_but_not_theme_vars(env) -> None:
    mod, root = env
    hit = _write(
        root / "frontend/src/components/Badge.tsx",
        "const a = <div style={{ color: '#1f2328', background: 'rgba(0,0,0,.5)' }} />;\n"
        "const good = { color: 'hsl(var(--foreground))', bg: 'var(--muted)' };\n",
    )
    out = mod.collect()
    assert [p for p in out] == [hit.relative_to(root).as_posix()]
    assert len(out[hit.relative_to(root).as_posix()]) == 1  # 仅硬编码行


def test_allowlist_definition_layers(env) -> None:
    mod, root = env
    _write(root / "frontend/src/index.css", "body { color: #123456; }\n")
    _write(root / "frontend/src/styles/design-tokens.css", "a { color: #abcdef; }\n")
    _write(root / "frontend/src/themes/preset.css", "a { color: #aaabb1; }\n")
    _write(root / "frontend/src/config/themes/presets/light.ts", "primary: '#0B1220',\n")
    _write(root / "frontend/src/config/themes/index.ts", "const hex = '#F4F7FB';\n")
    _write(root / "frontend/src/services/themeService.ts", "const x = '#102030';\n")
    _write(root / "frontend/src/services/themeValuePolicy.ts", "return '#ffffff'\n")
    _write(
        root / "frontend/src/stores/__tests__/helpers/storeTestMocks.ts",
        "primary: '#111827',\n",
    )
    _write(root / "frontend/src/components/A.test.tsx", "expect(c).toBe('#ffffff');\n")
    _write(root / "plugins/shared/dsh_adapter/plugin.json", '"t": "#010203"\n')
    assert mod.collect() == {}


def test_plugin_json_template_scanned(env) -> None:
    mod, root = env
    hit = _write(
        root / "plugins/shared/demo/plugin.json",
        '{"template": "<pre style=\\"color:#1f2328\\">清单</pre>"}\n',
    )
    out = mod.collect()
    assert [p for p in out] == [hit.relative_to(root).as_posix()]


def _run_main(mod, root: Path, baseline_text: str, *extra: str) -> int:
    baseline = root / "baseline.txt"
    baseline.write_text(baseline_text, encoding="utf-8", newline="\n")
    argv = ["check_theme_color_hygiene.py", "--baseline", str(baseline), *extra]
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sys, "argv", argv)
    try:
        return mod.main()
    finally:
        monkey.undo()


def test_baseline_overage_is_red(env, capsys) -> None:
    mod, root = env
    rel = "frontend/src/components/Badge.tsx"
    _write(root / rel, "const a = { color: '#111111' };\nconst b = { color: '#222222' };\n")
    assert _run_main(mod, root, f"{rel} 1\n", "--baseline-monotonic") == 1
    assert "超 baseline" in capsys.readouterr().out


def test_baseline_equal_is_green(env) -> None:
    mod, root = env
    rel = "frontend/src/components/Badge.tsx"
    _write(root / rel, "const a = { color: '#111111' };\n")
    assert _run_main(mod, root, f"{rel} 1\n", "--baseline-monotonic") == 0


def test_monotonic_stale_entry_is_red(env, capsys) -> None:
    mod, root = env
    rel = "frontend/src/components/Badge.tsx"
    _write(root / rel, "const a = { color: '#111111' };\n")  # 现 1 处，基线写 2
    assert _run_main(mod, root, f"{rel} 2\n", "--baseline-monotonic") == 1
    assert "待收紧" in capsys.readouterr().out


def test_monotonic_deleted_file_entry_is_red(env, capsys) -> None:
    mod, root = env
    assert _run_main(mod, root, "frontend/src/components/Gone.tsx 3\n", "--baseline-monotonic") == 1
    assert "待收紧" in capsys.readouterr().out
