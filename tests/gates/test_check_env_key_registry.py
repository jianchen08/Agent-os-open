# @feature: FP-GATE env 键登记门禁 | @ci: python-coverage
"""check_env_key_registry.py 单测（ADR 2026-11-17-kernel-env-key-registry-gate）。

行为契约（断输入→输出，不钉实现）：
- 注释提及不算读取面：/// 注释里的 AGENTOS_XXX 不产生登记义务（绿）；
- 代码字面量面（env::var 直读 / const 绑定）未登记 → 红且报 file:line；
- format! 模板构造（"AGENTOS_DYN_{}"）→ 按动态前缀归类，登记前缀后绿；
- 裸 "AGENTOS_"（保留名空间守卫 starts_with 形态）非键，不产生登记义务；
- 双向棘轮：源面未登记红 / 索引过期条目红 / description 缺失红；
- 索引 schema 非法（未知顶层节）→ fail-closed 非零退出；
- 负控制（防恒绿）：真实仓检查器子进程实跑 exit 0（73 键全登记基线）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_env_key_registry.py"

pytestmark = pytest.mark.unit

REGISTRY_ALL_GREEN = """\
production:
  AGENTOS_ALPHA:
    description: 测试旋钮甲。
  AGENTOS_BETA:
    description: 测试旋钮乙。
    default: 未设 = 关闭。
test_only:
  AGENTOS_TEST_FIX:
    description: 测试注入键。
dynamic_prefixes:
  - AGENTOS_DYN_
"""

RS_READ = 'fn main() {\n    let v = std::env::var("AGENTOS_ALPHA").ok();\n    let _ = v;\n}\n'


def _run(kernel_dir: Path, registry: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--kernel-dir", str(kernel_dir), "--registry", str(registry)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


def _make_tree(tmp_path: Path, registry_text: str, rs_files: dict[str, str]) -> tuple[Path, Path]:
    kernel = tmp_path / "kernel" / "crates"
    for rel, text in rs_files.items():
        target = kernel / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    registry = tmp_path / "config" / "kernel" / "env_keys.yaml"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(registry_text, encoding="utf-8")
    return kernel, registry


def test_comment_mentions_do_not_create_registration_burden(tmp_path: Path) -> None:
    # registry 刻意只含注释里那个键的反面——空 production：注释提及若被
    # 误提取，必然以"未登记"红；绿即证明注释不进提取面。
    kernel, registry = _make_tree(
        tmp_path,
        "production:\n",
        {"a/src/lib.rs": '/// 用 AGENTOS_SECRET_OF_DOCS 配置文档面（仅注释提及）。\nfn f() {}\n'},
    )
    assert _run(kernel, registry).returncode == 0, "注释提及不得产生登记义务"


def test_unregistered_literal_read_is_red_with_site(tmp_path: Path) -> None:
    kernel, registry = _make_tree(tmp_path, "production:\n  AGENTOS_ALPHA:\n    description: 甲。\n", {"a/src/lib.rs": RS_READ.replace("AGENTOS_ALPHA", "AGENTOS_OMEGA")})
    proc = _run(kernel, registry)
    assert proc.returncode == 1
    assert "AGENTOS_OMEGA" in proc.stdout
    assert "a/src/lib.rs" in proc.stdout, "违规必须报读取位点"


def test_unregistered_const_binding_is_red(tmp_path: Path) -> None:
    kernel, registry = _make_tree(
        tmp_path,
        "production:\n  AGENTOS_ALPHA:\n    description: 甲。\n",
        {"a/src/lib.rs": 'const KEY_ENV: &str = "AGENTOS_SIGMA";\n'},
    )
    assert _run(kernel, registry).returncode == 1


def test_dynamic_template_uses_prefix_registry(tmp_path: Path) -> None:
    rs = 'fn k(id: &str) -> String {\n    format!("AGENTOS_DYN_{}", id)\n}\n'
    # 未登记前缀：红。
    kernel, registry = _make_tree(tmp_path, "production:\n  AGENTOS_ALPHA:\n    description: 甲。\n", {"a/src/lib.rs": rs})
    proc = _run(kernel, registry)
    assert proc.returncode == 1
    assert "AGENTOS_DYN_" in proc.stdout
    # 登记前缀：绿（前缀条目豁免 description；registry 只含前缀免过期误红）。
    registry.write_text("dynamic_prefixes:\n  - AGENTOS_DYN_\n", encoding="utf-8")
    assert _run(kernel, registry).returncode == 0


def test_bare_agentos_namespace_guard_is_not_a_key(tmp_path: Path) -> None:
    kernel, registry = _make_tree(
        tmp_path,
        "production:\n",
        {"a/src/lib.rs": 'fn reserved(name: &str) -> bool {\n    name.to_uppercase().starts_with("AGENTOS_")\n}\n'},
    )
    assert _run(kernel, registry).returncode == 0, "保留名空间守卫不是 env 键"


def test_stale_registry_entry_is_red(tmp_path: Path) -> None:
    kernel, registry = _make_tree(
        tmp_path,
        "production:\n  AGENTOS_ALPHA:\n    description: 甲。\n  AGENTOS_GONE:\n    description: 源面已读不到。\n",
        {"a/src/lib.rs": RS_READ},
    )
    proc = _run(kernel, registry)
    assert proc.returncode == 1
    assert "过期索引条目 AGENTOS_GONE" in proc.stdout


def test_missing_description_is_red(tmp_path: Path) -> None:
    kernel, registry = _make_tree(
        tmp_path,
        "production:\n  AGENTOS_ALPHA:\n    default: 有默认无描述。\n",
        {"a/src/lib.rs": RS_READ},
    )
    proc = _run(kernel, registry)
    assert proc.returncode == 1
    assert "description" in proc.stdout, "空壳登记必须红"


def test_bad_schema_fails_closed(tmp_path: Path) -> None:
    kernel, registry = _make_tree(
        tmp_path,
        "no_such_section:\n  AGENTOS_ALPHA:\n    description: 甲。\n",
        {"a/src/lib.rs": RS_READ},
    )
    proc = _run(kernel, registry)
    assert proc.returncode != 0
    assert "未知顶层节" in proc.stdout + proc.stderr, "schema 非法必须 fail-closed 报位点"


def test_green_world(tmp_path: Path) -> None:
    kernel, registry = _make_tree(
        tmp_path,
        REGISTRY_ALL_GREEN,
        {
            "a/src/lib.rs": RS_READ + '\nconst OTHER: &str = "AGENTOS_BETA";\n',
            "a/src/lib/tests.rs": 'std::env::set_var("AGENTOS_TEST_FIX", "1");\n',
            "a/src/lib/gen.rs": 'let k = format!("AGENTOS_DYN_{}", id);\n',
        },
    )
    proc = _run(kernel, registry)
    assert proc.returncode == 0, proc.stdout


def test_negative_control_real_repo_green() -> None:
    """防恒绿：真实仓 73 键全登记基线，检查器必须绿。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"真实仓门禁红（登记索引漂移？）：\n{proc.stdout}\n{proc.stderr}"
