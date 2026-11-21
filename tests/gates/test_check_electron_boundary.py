# @feature: FP-GATE Electron 导入边界闸 | @ci: python-coverage
"""check_electron_boundary.py 单测。

行为契约（断输入→输出，不钉实现）：
- R1 目录互导：electron 树导入 frontend 红、frontend 导入 electron 树红；
  路径段含 "electron" 字样但词法归一后落 frontend 树（``../types/electron``）
  不红（防误报关键例）；
- R2 渲染侧 electron 包运行时引用：``from "electron"`` / require / 动态
  import() / ``@electron/remote`` / ``window.require(...)`` 全红；
  ``import type ... from "electron"``（含多行 specifier 列表）豁免绿；
- R3 preload 最小面：导入本地兄弟模块红；import "electron" 与 Node 内置绿；
- 形态覆盖：副作用 import "x" 与 export ... from 同样命中；
- 注释剥离：文档注释里的 frontend 路径字样不误报；
- fail-closed：扫描根缺失红（exit 2）、文件非 UTF-8 红码（exit 2）；
- 负控制（防恒绿）：真实仓全树实跑 exit 0（现状双向零导入）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_electron_boundary", SCRIPTS / "check_electron_boundary.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    return _load()


def _make_repo(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
    return tmp_path


def _run(mod, repo: Path, capsys) -> tuple[int, str]:
    rc = mod.main(["--root", str(repo)])
    return rc, capsys.readouterr().out


CLEAN_BASE = {
    "electron/main.ts": 'import { app } from "electron";\nimport { spawn } from "child_process";\nimport { relay } from "./ws-relay";\n',
    "electron/preload.ts": 'import { contextBridge, ipcRenderer } from "electron";\n',
    "frontend/src/services/api/foo.ts": 'import { ref } from "vue";\nimport { local } from "./local";\n',
}


# ── 干净树 ───────────────────────────────────────────────────────────────


def test_clean_tree_passes(mod, tmp_path, capsys):
    repo = _make_repo(tmp_path, CLEAN_BASE)
    rc, out = _run(mod, repo, capsys)
    assert rc == 0
    assert "通过" in out


# ── R1 目录互导 ──────────────────────────────────────────────────────────


def test_r1_electron_imports_frontend(mod, tmp_path, capsys):
    repo = _make_repo(tmp_path, {**CLEAN_BASE, "electron/main.ts": 'import { x } from "../frontend/src/utils/foo";\n'})
    rc, out = _run(mod, repo, capsys)
    assert rc == 1
    assert "R1" in out and "electron/main.ts" in out


def test_r1_frontend_imports_electron_tree(mod, tmp_path, capsys):
    repo = _make_repo(tmp_path, {**CLEAN_BASE, "frontend/src/services/api/foo.ts": 'import { spawn } from "../../../../electron/kernel-manager";\n'})
    rc, out = _run(mod, repo, capsys)
    assert rc == 1
    assert "R1" in out and "frontend/src/services/api/foo.ts" in out


def test_r1_types_electron_relative_is_not_the_electron_tree(mod, tmp_path, capsys):
    # 防误报关键例：路径段含 electron 字样，归一后落 frontend 树
    repo = _make_repo(
        tmp_path,
        {
            **CLEAN_BASE,
            "frontend/src/types/electron.d.ts": "export interface ElectronAPI { x: number }\n",
            "frontend/src/pages/a.tsx": 'import type { ElectronAPI } from "../types/electron";\n',
        },
    )
    rc, out = _run(mod, repo, capsys)
    assert rc == 0


# ── R2 渲染侧 electron 包运行时引用 ─────────────────────────────────────


def _r2_case(spec_line: str) -> dict[str, str]:
    return {
        "electron/main.ts": 'import { app } from "electron";\n',
        "frontend/src/services/api/foo.ts": spec_line,
    }


@pytest.mark.parametrize(
    "line",
    [
        'import { ipcRenderer } from "electron";\n',
        'const { remote } = require("electron");\n',
        'const m = await import("electron");\n',
        'import "@electron/remote";\n',
        'const fs = window.require("fs");\n',
    ],
)
def test_r2_runtime_electron_references_fail(mod, tmp_path, capsys, line):
    repo = _make_repo(tmp_path, _r2_case(line))
    rc, out = _run(mod, repo, capsys)
    assert rc == 1
    assert "R2" in out


def test_r2_type_only_import_exempt(mod, tmp_path, capsys):
    repo = _make_repo(tmp_path, _r2_case('import type { ElectronAPI } from "electron";\nexport type { ElectronAPI };\n'))
    rc, _ = _run(mod, repo, capsys)
    assert rc == 0


def test_r2_multiline_type_only_import_exempt(mod, tmp_path, capsys):
    line = 'import type {\n  BrowserWindow,\n  IpcRendererEvent,\n} from "electron";\n'
    repo = _make_repo(tmp_path, _r2_case(line))
    rc, _ = _run(mod, repo, capsys)
    assert rc == 0


# ── R3 preload 最小面 ────────────────────────────────────────────────────


def test_r3_preload_local_import_fails(mod, tmp_path, capsys):
    repo = _make_repo(
        tmp_path,
        {
            **CLEAN_BASE,
            "electron/kernel-manager.ts": "export const CHILD = 1;\n",
            "electron/preload.ts": 'import { CHILD } from "./kernel-manager";\nimport { contextBridge } from "electron";\n',
        },
    )
    rc, out = _run(mod, repo, capsys)
    assert rc == 1
    assert "R3" in out


def test_r3_preload_node_builtin_passes(mod, tmp_path, capsys):
    repo = _make_repo(
        tmp_path,
        {
            **CLEAN_BASE,
            "electron/preload.ts": 'import { readFileSync } from "fs";\nimport { contextBridge } from "electron";\n',
        },
    )
    rc, _ = _run(mod, repo, capsys)
    assert rc == 0


# ── 形态覆盖与注释剥离 ───────────────────────────────────────────────────


def test_side_effect_and_export_from_forms_hit(mod, tmp_path, capsys):
    repo = _make_repo(
        tmp_path,
        {
            **CLEAN_BASE,
            "electron/main.ts": 'import "../frontend/src/polyfill";\nexport { x } from "../frontend/src/y";\n',
        },
    )
    rc, out = _run(mod, repo, capsys)
    assert rc == 1
    assert out.count("R1") == 2


def test_comments_do_not_trigger(mod, tmp_path, capsys):
    repo = _make_repo(
        tmp_path,
        {
            # 文档注释里提及 frontend 与 electron 路径不误报
            "electron/main.ts": '// 与 frontend/src/types/electron.d.ts 保持一致\n/* import { x } from "../frontend/src/deep"; */\nconst a = 1;\n',
            "frontend/src/services/api/foo.ts": '// 参考 electron/main.ts 与 window.require 用法\nconst b = 2;\n',
        },
    )
    rc, _ = _run(mod, repo, capsys)
    assert rc == 0


def test_function_body_lazy_import_also_hits(mod, tmp_path, capsys):
    # 函数体内惰性 require 同样是运行时引用（绕过面覆盖）
    repo = _make_repo(
        tmp_path,
        {
            "electron/main.ts": 'import { app } from "electron";\n',
            "frontend/src/services/api/foo.ts": 'export async function lazy() {\n  const { remote } = require("electron");\n  return remote;\n}\n',
        },
    )
    rc, out = _run(mod, repo, capsys)
    assert rc == 1
    assert "R2" in out


# ── fail-closed ──────────────────────────────────────────────────────────


def test_missing_scan_root_fails_closed(mod, tmp_path, capsys):
    repo = _make_repo(tmp_path, {"electron/main.ts": 'import { app } from "electron";\n'})
    rc, out = _run(mod, repo, capsys)
    assert rc == 2
    assert "fail-closed" in out


def test_unreadable_file_fails_closed(mod, tmp_path, capsys):
    repo = _make_repo(
        tmp_path,
        {"electron/main.ts": b"\xff\xfe\x00broken-utf8", "frontend/src/services/api/foo.ts": "const a = 1;\n"},
    )
    rc, out = _run(mod, repo, capsys)
    assert rc == 2
    assert "fail-closed" in out


# ── 负控制（防恒绿）：真实仓实跑 ─────────────────────────────────────────


def test_real_repo_passes(mod):
    assert mod.main([]) == 0
