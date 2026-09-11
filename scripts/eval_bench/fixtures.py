"""评测期望值 fixture 计算器：断言期望 = 仓库真值现场计算。

能力组任务的产物（统计值/清单/摘录）以 harness 本地实算结果为唯一真值，
suite 不写死数字（防仓库演化后期望漂移，也防模型编造数字蒙混——编造值与
实算值对不上即红）。

所有路径以仓库根为基准（与内核 sidecar 的 cwd 语义无关，本文件只在
harness 进程内运行）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

# scripts/eval_bench/fixtures.py → 仓库根
ROOT = Path(__file__).resolve().parents[2]


def _repo_glob(pattern: str) -> list[Path]:
    return sorted(ROOT.glob(pattern))


def glob_file_count(pattern: str) -> int:
    """仓库根 glob 模式命中的文件数。"""
    return sum(1 for p in _repo_glob(pattern) if p.is_file())


def glob_line_count(pattern: str) -> int:
    """命中文件的文本行数之和（每文件按 splitlines 计）。"""
    total = 0
    for p in _repo_glob(pattern):
        if p.is_file():
            try:
                total += len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                continue
    return total


def yaml_list_len(path: str, key: str) -> int:
    """仓库 YAML 文件中顶层列表键的长度。"""
    data = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    value = data.get(key) if isinstance(data, dict) else None
    return len(value) if isinstance(value, list) else 0


def llm_model_ids(providers: list[str]) -> list[str]:
    """config/models/llm.yaml 中 provider ∈ providers 的模型 id（升序）。"""
    data = yaml.safe_load((ROOT / "config" / "models" / "llm.yaml").read_text(encoding="utf-8"))
    models = data.get("models", {}) if isinstance(data, dict) else {}
    return sorted(
        model_id
        for model_id, conf in models.items()
        if isinstance(conf, dict) and conf.get("provider") in providers
    )


def file_line(path: str, lineno: int) -> str:
    """仓库文本文件第 lineno 行（1 起，去首尾空白）；越界返回空串。"""
    lines = (ROOT / path).read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""


FIXTURES: dict[str, Callable[..., Any]] = {
    "glob_file_count": glob_file_count,
    "glob_line_count": glob_line_count,
    "yaml_list_len": yaml_list_len,
    "llm_model_ids": llm_model_ids,
    "file_line": file_line,
}


def resolve_fixture(spec: dict[str, Any]) -> Any:
    """按 suite 声明解析期望值：{"fixture": 名称, "args": {...}}。"""
    name = spec.get("fixture")
    if name not in FIXTURES:
        raise SystemExit(f"未知 fixture: {name}（已知: {sorted(FIXTURES)}）")
    return FIXTURES[name](**(spec.get("args") or {}))
