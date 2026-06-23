"""
schema_evaluator 路径解析回归测试

覆盖 BUG-FIX-fix_20260606_format_valid_workspace_path:
相对 path + workspace 参数应正确拼接解析，不再相对进程 cwd。

覆盖 BUG-FIX-fix_20260607_format_valid_json_default:
format=auto 时应根据文件扩展名自动检测格式，不再默认 json 导致非 JSON 文件校验失败。

场景：
1. 相对 path + workspace（正常）：应能在 workspace 子目录找到文件
2. 绝对 path：忽略 workspace，按绝对路径读取
3. 相对 path 无 workspace：维持原 cwd 行为（兼容旧调用）
4. auto 格式 + YAML 文件：应自动检测为 yaml 格式校验
5. auto 格式 + JSON 文件：应自动检测为 json 格式校验
6. auto 格式 + Markdown 文件：应自动检测为 regex 格式
7. 显式 format=json + YAML 文件：应按 json 校验（允许失败）
8. auto 格式 + data 参数：应按数据类型推断
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.builtin.evaluators.schema_evaluator import SchemaEvaluator


@pytest.fixture
def evaluator() -> SchemaEvaluator:
    return SchemaEvaluator()


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """模拟容器任务工作空间：在 tmp_path/data 下放合法 JSON。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    payload = {"test": "path_fix", "status": "ok"}
    (data_dir / "format_valid_path_test.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_relative_path_with_workspace_resolves_correctly(
    evaluator: SchemaEvaluator, workspace_dir: Path,
) -> None:
    """相对 path + workspace 应拼接到 workspace 下读取，不再相对 cwd。"""
    result = await evaluator.execute({
        "path": "data/format_valid_path_test.json",
        "format": "json",
        "workspace": str(workspace_dir),
    })

    assert result.success is True
    assert result.output is not None
    assert result.output["passed"] is True
    assert result.output["score"] == 100


@pytest.mark.asyncio
async def test_absolute_path_ignores_workspace(
    evaluator: SchemaEvaluator, workspace_dir: Path,
) -> None:
    """绝对 path 应绕过 workspace 拼接，按绝对路径读取。"""
    abs_path = workspace_dir / "data" / "format_valid_path_test.json"
    result = await evaluator.execute({
        "path": str(abs_path),
        "format": "json",
        "workspace": "/some/other/workspace",
    })

    assert result.success is True
    assert result.output["passed"] is True


@pytest.mark.asyncio
async def test_missing_file_in_workspace_reports_not_found(
    evaluator: SchemaEvaluator, workspace_dir: Path,
) -> None:
    """workspace 下文件不存在时返回 passed=False，不抛异常。"""
    result = await evaluator.execute({
        "path": "data/nonexistent.json",
        "format": "json",
        "workspace": str(workspace_dir),
    })

    assert result.success is True  # 业务结果成功，结果由 passed 字段表达
    assert result.output["passed"] is False
    assert "文件不存在" in result.output["feedback"]


@pytest.mark.asyncio
async def test_relative_path_without_workspace_falls_back_to_cwd(
    evaluator: SchemaEvaluator,
) -> None:
    """无 workspace 时维持旧行为：相对 path 相对 cwd（不破坏既有调用）。"""
    # 故意找一个绝对不会存在的相对路径，确保走的是 cwd 分支
    result = await evaluator.execute({
        "path": "definitely/not/exist/__no_workspace__.json",
        "format": "json",
    })

    assert result.success is True
    assert result.output["passed"] is False
    assert "文件不存在" in result.output["feedback"]


@pytest.mark.asyncio
async def test_data_param_takes_precedence_over_path(
    evaluator: SchemaEvaluator,
) -> None:
    """data 参数存在时不应走文件读取路径，workspace 不影响。"""
    result = await evaluator.execute({
        "data": '{"k": "v"}',
        "path": "should/be/ignored.json",
        "format": "json",
        "workspace": "/does/not/matter",
    })

    assert result.success is True
    assert result.output["passed"] is True


# ========== BUG-FIX-fix_20260607_format_valid_json_default 测试 ==========


@pytest.fixture
def multi_format_workspace(tmp_path: Path) -> Path:
    """创建包含多种格式文件的工作空间。"""
    # JSON 文件
    (tmp_path / "config.json").write_text(
        json.dumps({"name": "test", "version": 1}), encoding="utf-8",
    )
    # YAML 文件
    (tmp_path / "config.yaml").write_text(
        "name: test\nversion: 1\n", encoding="utf-8",
    )
    (tmp_path / "config.yml").write_text(
        "name: test2\nversion: 2\n", encoding="utf-8",
    )
    # Markdown 文件
    (tmp_path / "report.md").write_text(
        "# Report\n\nThis is a test report.\n", encoding="utf-8",
    )
    return tmp_path


@pytest.mark.asyncio
async def test_auto_format_detects_yaml_from_extension(
    evaluator: SchemaEvaluator, multi_format_workspace: Path,
) -> None:
    """format=auto 时，.yaml 文件应自动检测为 yaml 格式校验，不再以 json 校验失败。"""
    result = await evaluator.execute({
        "path": "config.yaml",
        "format": "auto",
        "workspace": str(multi_format_workspace),
    })

    assert result.success is True
    assert result.output["passed"] is True
    assert result.output["score"] == 100
    assert "YAML" in result.output["feedback"]


@pytest.mark.asyncio
async def test_auto_format_detects_yml_as_yaml(
    evaluator: SchemaEvaluator, multi_format_workspace: Path,
) -> None:
    """format=auto 时，.yml 文件应自动检测为 yaml 格式。"""
    result = await evaluator.execute({
        "path": "config.yml",
        "format": "auto",
        "workspace": str(multi_format_workspace),
    })

    assert result.success is True
    assert result.output["passed"] is True
    assert "YAML" in result.output["feedback"]


@pytest.mark.asyncio
async def test_auto_format_detects_json_from_extension(
    evaluator: SchemaEvaluator, multi_format_workspace: Path,
) -> None:
    """format=auto 时，.json 文件应自动检测为 json 格式校验。"""
    result = await evaluator.execute({
        "path": "config.json",
        "format": "auto",
        "workspace": str(multi_format_workspace),
    })

    assert result.success is True
    assert result.output["passed"] is True
    assert "JSON" in result.output["feedback"]


@pytest.mark.asyncio
async def test_auto_format_detects_md_as_regex(
    evaluator: SchemaEvaluator, multi_format_workspace: Path,
) -> None:
    """format=auto 时，.md 文件应自动检测为 regex 格式（非 JSON 校验）。"""
    result = await evaluator.execute({
        "path": "report.md",
        "format": "auto",
        "workspace": str(multi_format_workspace),
    })

    assert result.success is True
    # regex 格式需要 patterns 参数，无 patterns 时应返回 passed=False
    assert result.output["passed"] is False
    assert "patterns" in result.output["feedback"]


@pytest.mark.asyncio
async def test_explicit_json_format_on_yaml_file_still_fails(
    evaluator: SchemaEvaluator, multi_format_workspace: Path,
) -> None:
    """显式指定 format=json 时，YAML 文件仍以 json 校验（允许失败，保持向后兼容）。"""
    result = await evaluator.execute({
        "path": "config.yaml",
        "format": "json",
        "workspace": str(multi_format_workspace),
    })

    assert result.success is True
    assert result.output["passed"] is False
    assert "JSON 格式无效" in result.output["feedback"]


@pytest.mark.asyncio
async def test_auto_format_with_dict_data_infers_json(
    evaluator: SchemaEvaluator,
) -> None:
    """format=auto + data 为 dict 时，应推断为 json 格式。"""
    result = await evaluator.execute({
        "data": {"key": "value"},
        "format": "auto",
    })

    assert result.success is True
    assert result.output["passed"] is True
    assert "JSON" in result.output["feedback"]


@pytest.mark.asyncio
async def test_auto_format_with_json_string_infers_json(
    evaluator: SchemaEvaluator,
) -> None:
    """format=auto + data 为 JSON 字符串时，应推断为 json 格式。"""
    result = await evaluator.execute({
        "data": '{"key": "value"}',
        "format": "auto",
    })

    assert result.success is True
    assert result.output["passed"] is True
    assert "JSON" in result.output["feedback"]


@pytest.mark.asyncio
async def test_auto_format_with_yaml_string_infers_yaml(
    evaluator: SchemaEvaluator,
) -> None:
    """format=auto + data 为纯 YAML 字符串（非 JSON）时，应推断为 yaml 格式。"""
    result = await evaluator.execute({
        "data": "name: test\nversion: 1\n",
        "format": "auto",
    })

    assert result.success is True
    assert result.output["passed"] is True
    assert "YAML" in result.output["feedback"]


@pytest.mark.asyncio
async def test_detect_format_method_directly() -> None:
    """直接测试 _detect_format 方法的各种场景。"""
    ev = SchemaEvaluator()

    # 文件扩展名检测
    assert ev._detect_format("config.json", None) == "json"
    assert ev._detect_format("config.yaml", None) == "yaml"
    assert ev._detect_format("config.yml", None) == "yaml"
    assert ev._detect_format("report.md", None) == "regex"
    assert ev._detect_format("style.css", None) == "regex"
    assert ev._detect_format("app.py", None) == "regex"

    # dict/list 数据 → json
    assert ev._detect_format(None, {"key": "val"}) == "json"
    assert ev._detect_format(None, [1, 2, 3]) == "json"

    # JSON 字符串 → json
    assert ev._detect_format(None, '{"key": "val"}') == "json"

    # YAML 字符串（非 JSON）→ yaml
    assert ev._detect_format(None, "name: test\nversion: 1\n") == "yaml"

    # 未知扩展名 + 非结构化文本 → regex
    assert ev._detect_format("unknown.xyz", "plain text") == "regex"
