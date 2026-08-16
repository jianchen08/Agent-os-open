# @feature: FP-0.2.三 宿主接入 | @vision: V3 可嵌入 | @ci: python-coverage
"""channel_cli._PROJECT_ROOT 路径深度回归测试（F-CLI-1）。

意图：cli_main.py 迁移到 plugins/shared/system/channel_cli/ 后，
`_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent` 4 级
parent 只解析到 plugins/（parents[3]），导致默认管道配置
config/pipelines/default.yaml 加载失败；fallback（3 级 parent）同样错。

本测试锁定两处路径常量的**解析结果**必须落在仓库根（存在 config/pipelines/），
而不是硬编码期望的字符串——路径计算逻辑演进时，测试仍能守住「结果正确」这一意图。

cli_main 顶层依赖兄弟模块（cli_interactive → infrastructure.*），在测试环境
无法直接 import（见 cli_main.py:70-72 的 DEBT 注释），因此用 AST 提取源码中的
真实赋值表达式并求值，仍以真实文件位置驱动解析。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_MAIN = _REPO_ROOT / "plugins" / "shared" / "system" / "channel_cli" / "cli_main.py"


def _eval_assignments() -> dict[str, Path]:
    """从 cli_main.py 源码提取并求值 `_PROJECT_ROOT` 与 fallback `project_root` 赋值表达式。"""
    text = _CLI_MAIN.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(_CLI_MAIN))
    ns = {"__file__": str(_CLI_MAIN.resolve()), "Path": Path}
    found: dict[str, Path] = {}

    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_PROJECT_ROOT" for t in node.targets
        ):
            found["_PROJECT_ROOT"] = Path(
                eval(compile(ast.Expression(node.value), str(_CLI_MAIN), "eval"), ns)
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "project_root" for t in node.targets
        ):
            found["project_root"] = Path(
                eval(compile(ast.Expression(node.value), str(_CLI_MAIN), "eval"), ns)
            )

    return found


def test_project_root_resolves_to_repo_root() -> None:
    """意图：默认管道配置路径必须指向仓库根 config/pipelines/default.yaml，
    否则 CLI 启动 setup_pipeline() 直接 FileNotFoundError。"""
    found = _eval_assignments()
    assert "_PROJECT_ROOT" in found, "cli_main.py 中未找到 _PROJECT_ROOT 赋值"

    root = found["_PROJECT_ROOT"]
    assert root == _REPO_ROOT
    assert (root / "config" / "pipelines").is_dir()


def test_project_root_fallback_resolves_to_repo_root_config() -> None:
    """意图：:262 的 fallback 与主常量同级——若默认配置缺失，fallback 也必须
    落在仓库根 config/pipelines/ 而不是 plugins/shared/ 下的死路径。"""
    found = _eval_assignments()
    assert "project_root" in found, "cli_main.py 中未找到 fallback project_root 赋值"

    fallback = found["project_root"]
    assert fallback == _REPO_ROOT / "config" / "pipelines" / "default.yaml"
    assert fallback.exists()
