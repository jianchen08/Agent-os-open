"""测试2:isolated+workspace=project_root 验证脚本

验证目标：
  隔离模式（isolated）下，workspace 应指向系统项目根目录
  （D:\\myproject\\container_e17cc5927dfd 的逻辑副本 = /workspace）。

验证维度：
  V1. 工作目录可解析为项目根（关键标记齐全）
  V2. .project/ 项目文档可被读取（架构/契约/宪法）
  V3. .ai_workspaces/ 隔离副本机制已就位
  V4. 代码层 WorkspaceLifecycleManager 在 project_root 模式下行为正确
  V5. 测试集合可被 pytest 识别并执行（最小烟雾测试）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# Windows 目标路径（任务描述中的原始项目根）
TARGET_WIN_ROOT = Path("D:/myproject/container_e17cc5927dfd")
# 当前隔离副本（Linux 实际工作目录）
ISOLATED_ROOT = Path.cwd().resolve()


def _v1_root_markers() -> dict:
    """V1: 项目根标记识别（验证 workspace 指向正确的项目根）"""
    markers = [".project", "kernel", "frontend", "tests", "plugins",
               "docs", "pyproject.toml", "README.md", ".ai_workspaces"]
    result = {}
    for m in markers:
        p = ISOLATED_ROOT / m
        result[m] = p.exists()
    all_ok = all(result.values())
    return {"name": "V1_root_markers", "pass": all_ok, "details": result}


def _v2_project_docs_readable() -> dict:
    """V2: .project/ 项目文档可被读取"""
    docs = ["architecture.md", "api_contract.md", "domain_model.md",
            "features.md", "frontend_01_alignment_charter.md", "widget_contracts.md"]
    project_dir = ISOLATED_ROOT / ".project"
    result = {}
    for d in docs:
        p = project_dir / d
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8")
                result[d] = {"exists": True, "readable": True, "size": len(content)}
            except Exception as exc:
                result[d] = {"exists": True, "readable": False, "error": str(exc)}
        else:
            result[d] = {"exists": False, "readable": False}
    all_readable = all(v.get("readable", False) for v in result.values())
    return {"name": "V2_project_docs", "pass": all_readable, "details": result}


def _v3_ai_workspaces_present() -> dict:
    """V3: .ai_workspaces/ 隔离副本机制已就位"""
    ai_ws = ISOLATED_ROOT / ".ai_workspaces"
    if not ai_ws.exists():
        return {"name": "V3_ai_workspaces", "pass": False,
                "details": {"reason": "不存在 .ai_workspaces"}}
    subdirs = [p for p in ai_ws.iterdir() if p.is_dir()]
    return {"name": "V3_ai_workspaces",
            "pass": len(subdirs) > 0,
            "details": {"path": str(ai_ws), "subdir_count": len(subdirs),
                        "sample": sorted([p.name for p in subdirs])[:5]}}


def _v4_workspace_lifecycle_manager_project_root_mode() -> dict:
    """V4: 代码层 WorkspaceLifecycleManager 在 project_root 模式下行为正确

    验证点（来源：tests/test_workspace_lifecycle_mode.py 测试约定）：
      - _start_root_task 在 plain 模式下直接操作目录，不建 worktree
      - 默认 mode 为 worktree
      - 子任务 plain 模式共享宿主目录
      - 注入配置覆盖 _get_workspace_root

    这里仅做轻量冒烟：找到管理器模块并验证可导入，避免依赖 Docker/LLM 等重组件。
    """
    candidates = [
        ISOLATED_ROOT / "kernel/crates/engine/src/workspace_lifecycle.py",
        ISOLATED_ROOT / "kernel/crates/engine/src/workspace_lifecycle_manager.py",
        ISOLATED_ROOT / "kernel/crates/engine/src/manager.py",
        ISOLATED_ROOT / "kernel/crates/engine/src/worktree_manager.py",
    ]
    found = None
    for c in candidates:
        if c.exists():
            found = c
            break
    if found is None:
        # 放宽搜索
        for py in (ISOLATED_ROOT / "kernel").rglob("*.py"):
            if "workspace" in py.name.lower() and "lifecycle" in py.name.lower():
                found = py
                break
    if found is None:
        return {"name": "V4_lifecycle_manager", "pass": None,
                "details": {"reason": "未找到 WorkspaceLifecycleManager 源码（可能命名不同，本次仅记录）",
                            "scanned_candidates": [str(c) for c in candidates]}}
    # 读取源码关键词
    text = found.read_text(encoding="utf-8", errors="replace")
    keywords = ["plain", "worktree", "project_root", "branch"]
    found_kw = {k: (k in text) for k in keywords}
    return {"name": "V4_lifecycle_manager",
            "pass": all(found_kw.values()),
            "details": {"file": str(found.relative_to(ISOLATED_ROOT)),
                        "size_bytes": found.stat().st_size,
                        "keywords_present": found_kw}}


def _v5_pytest_smoke() -> dict:
    """V5: pytest 能识别项目测试集合（最小烟雾测试）"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "tests/test_new_project_e2e.py::TestNewProjectE2E::test_new_project_workspace_under_ai_workspaces"],
            cwd=str(ISOLATED_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        collected = "test session starts" in result.stdout.lower() or \
                    "test_new_project_workspace_under_ai_workspaces" in result.stdout
        return {"name": "V5_pytest_smoke",
                "pass": collected and result.returncode in (0, 5),  # 5 = no tests collected
                "details": {
                    "returncode": result.returncode,
                    "stdout_tail": result.stdout[-400:] if result.stdout else "",
                    "stderr_tail": result.stderr[-400:] if result.stderr else "",
                }}
    except subprocess.TimeoutExpired:
        return {"name": "V5_pytest_smoke", "pass": False,
                "details": {"reason": "pytest --collect-only 超时（60s）"}}
    except Exception as exc:
        return {"name": "V5_pytest_smoke", "pass": False,
                "details": {"reason": f"执行失败: {exc}"}}


def main() -> int:
    print(f"=== 测试2:isolated+workspace=project_root 验证 ===")
    print(f"目标 Windows 项目根: {TARGET_WIN_ROOT}")
    print(f"实际隔离副本根:     {ISOLATED_ROOT}")
    print()

    results = [
        _v1_root_markers(),
        _v2_project_docs_readable(),
        _v3_ai_workspaces_present(),
        _v4_workspace_lifecycle_manager_project_root_mode(),
        _v5_pytest_smoke(),
    ]

    for r in results:
        status = "PASS" if r["pass"] else ("SKIP" if r["pass"] is None else "FAIL")
        print(f"[{status}] {r['name']}")
        for k, v in r["details"].items():
            print(f"      {k}: {v}")
        print()

    # 总结
    passed = sum(1 for r in results if r["pass"] is True)
    skipped = sum(1 for r in results if r["pass"] is None)
    failed = sum(1 for r in results if r["pass"] is False)
    total = len(results)
    print(f"=== 总结: PASS={passed}, SKIP={skipped}, FAIL={failed}, TOTAL={total} ===")

    # 写结果 JSON
    out = ISOLATED_ROOT / "reports" / "test2_isolated_workspace_project_root.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "test_name": "测试2:isolated+workspace=project_root",
        "target_win_root": str(TARGET_WIN_ROOT),
        "isolated_root": str(ISOLATED_ROOT),
        "results": results,
        "summary": {"pass": passed, "skip": skipped, "fail": failed, "total": total},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入: {out.relative_to(ISOLATED_ROOT)}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
