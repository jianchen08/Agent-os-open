#!/usr/bin/env python
"""Agent OS 统一启动脚本。

支持多种运行模式：
- demo: Demo 模式（echo 回显，无需 API Key）
- real: 真实 LLM 模式（需配置 API Key）
- e2e: 端到端功能测试
- pytest: 单元/集成测试
- llm-test: 真实 LLM 调用测试

使用方式::

    python run.py demo           # Demo 模式
    python run.py real           # 真实 LLM 模式
    python run.py e2e            # E2E 测试
    python run.py pytest         # pytest 测试
    python run.py llm-test       # LLM 真实调用测试
    python run.py all            # 运行所有测试
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path

# 确保源码路径
PROJECT_ROOT = Path(__file__).parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


def run_demo():
    """启动 Demo 模式 CLI（echo 回显）。"""
    from channels.cli.cli_main import CLIApplication
    print("=" * 50)
    print("Agent OS CLI — Demo 模式")
    print("（echo 回显，无需 API Key）")
    print("输入 quit 或 exit 退出")
    print("=" * 50)
    app = CLIApplication()
    app.setup_pipeline()
    try:
        asyncio.run(app.run())
    finally:
        try:
            from llm.adapter import cleanup_litellm_logging
            cleanup_litellm_logging()
        except Exception:
            pass


def run_real(config_path: str | None = None):
    """启动真实 LLM 模式 CLI。"""
    from channels.cli.cli_main import CLIApplication
    print("=" * 50)
    print("Agent OS CLI — 真实 LLM 模式")
    print("（需配置 API Key）")
    print("输入 quit 或 exit 退出")
    print("=" * 50)
    app = CLIApplication()
    app.setup_pipeline(config_path=config_path)
    try:
        asyncio.run(app.run())
    finally:
        try:
            from llm.adapter import cleanup_litellm_logging
            cleanup_litellm_logging()
        except Exception:
            pass


def run_e2e():
    """运行端到端功能测试。"""
    print("=" * 50)
    print("Agent OS E2E 功能测试")
    print("=" * 50)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/e2e", "--tb=short", "-q"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    return result.returncode


def run_pytest(args: list[str] | None = None):
    """运行 pytest 测试。"""
    print("=" * 50)
    print("Agent OS Pytest 测试")
    print("=" * 50)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"]
    if args:
        cmd.extend(args)
    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    return result.returncode


def run_llm_test():
    """运行真实 LLM 调用测试（带 requires_api marker）。"""
    print("=" * 50)
    print("Agent OS LLM 真实调用测试")
    print("=" * 50)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", "requires_api", "--tb=short", "-q"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    return result.returncode


def main():
    parser = argparse.ArgumentParser(
        description="Agent OS 统一启动脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run.py demo           # Demo 模式（echo 回显）
  python run.py real           # 真实 LLM 模式
  python run.py real -c config/pipelines/default.yaml  # 指定配置
  python run.py e2e            # E2E 测试
  python run.py pytest         # pytest 单元测试
  python run.py llm-test       # LLM 真实调用测试
  python run.py all            # 运行所有测试
        """,
    )
    parser.add_argument(
        "mode",
        choices=["demo", "real", "e2e", "pytest", "llm-test", "all"],
        help="运行模式",
    )
    parser.add_argument(
        "-c", "--config",
        type=str,
        default=None,
        help="管道配置 YAML 路径（仅 real 模式）",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用调试日志",
    )

    args = parser.parse_args()

    if args.debug:
        import logging
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    if args.mode == "demo":
        run_demo()
    elif args.mode == "real":
        run_real(config_path=args.config)
    elif args.mode == "e2e":
        rc = run_e2e()
        sys.exit(rc)
    elif args.mode == "pytest":
        rc = run_pytest()
        sys.exit(rc)
    elif args.mode == "llm-test":
        rc = run_llm_test()
        sys.exit(rc)
    elif args.mode == "all":
        print("\n>>> [1/3] 运行 pytest...")
        rc1 = run_pytest()
        print(f"\n>>> pytest 结果: {'PASS' if rc1 == 0 else 'FAIL'} (rc={rc1})")

        print("\n>>> [2/3] 运行 E2E 测试...")
        rc2 = run_e2e()
        print(f"\n>>> E2E 结果: {'PASS' if rc2 == 0 else 'FAIL'} (rc={rc2})")

        print("\n>>> [3/3] 运行 LLM 真实调用测试...")
        rc3 = run_llm_test()
        print(f"\n>>> LLM 测试结果: {'PASS' if rc3 == 0 else 'FAIL'} (rc={rc3})")

        print("\n" + "=" * 50)
        print("所有测试完成！")
        print("=" * 50)
        sys.exit(max(rc1, rc2, rc3))


if __name__ == "__main__":
    main()
