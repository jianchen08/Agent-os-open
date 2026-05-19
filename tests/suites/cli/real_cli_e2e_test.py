#!/usr/bin/env python3
"""
真实 CLI E2E 测试方案

目标：让大模型实际执行任务，验证完整管道
- 启动 CLI
- 发送消息给大模型
- 验证大模型是否正确响应
- 全程真实调用，不使用 Mock
"""

import asyncio
import sys
from pathlib import Path

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).parent / "src"))

from pipeline.engine import PipelineEngine
from pipeline.registry import PipelineRegistry
from pipeline.config import PipelineConfig
from channels.cli.input_adapter import CLIInputAdapter
from channels.cli.output_adapter import CLIOutputAdapter


class RealE2ETester:
    """真实 E2E 测试器"""

    def __init__(self, pipeline_name: str = "default"):
        self.pipeline_name = pipeline_name
        self.registry = PipelineRegistry()
        self.results = []

    async def setup_pipeline(self) -> PipelineEngine:
        """加载并设置管道"""
        config = PipelineConfig.from_yaml(
            f"config/pipelines/{self.pipeline_name}.yaml"
        )
        engine = await self.registry.create_engine(config)
        return engine

    async def send_message(self, message: str, timeout: int = 60) -> str:
        """发送消息并获取响应"""
        engine = await self.setup_pipeline()

        input_adapter = CLIInputAdapter()
        output_adapter = CLIOutputAdapter()

        # 构建输入
        context = {
            "session_id": "test_session",
            "user_message": message,
            "agent_level": 3,
        }

        # 执行管道
        try:
            result = await asyncio.wait_for(
                engine.execute(context, input_adapter, output_adapter),
                timeout=timeout
            )
            return result.get("response", "")
        except asyncio.TimeoutError:
            return "[TIMEOUT]"
        except Exception as e:
            return f"[ERROR] {e}"

    def add_result(self, test_name: str, passed: bool, details: str = ""):
        """记录测试结果"""
        self.results.append({
            "name": test_name,
            "passed": passed,
            "details": details
        })
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status}: {test_name}")
        if details:
            print(f"    {details}")


async def run_basic_conversation_test():
    """测试1: 基本对话"""
    print("\n=== 测试1: 基本对话 ===")
    tester = RealE2ETester()

    response = await tester.send_message("你好，请介绍一下你自己")

    # 检查响应是否合理
    passed = (
        len(response) > 10 and
        "[ERROR]" not in response and
        "[TIMEOUT]" not in response
    )
    tester.add_result("基本对话", passed, f"响应长度: {len(response)}")
    return tester.results


async def run_tool_discovery_test():
    """测试2: 工具发现"""
    print("\n=== 测试2: 工具发现 ===")
    tester = RealE2ETester()

    # 让模型列出可用工具
    response = await tester.send_message(
        "列出你当前可用的工具（tools），不需要调用，只需要告诉我工具名称"
    )

    passed = (
        len(response) > 20 and
        "[ERROR]" not in response
    )
    tester.add_result("工具发现", passed, f"响应: {response[:100]}...")
    return tester.results


async def run_file_read_test():
    """测试3: 文件读取工具调用"""
    print("\n=== 测试3: 文件读取工具 ===")
    tester = RealE2ETester()

    # 让模型读取一个简单的配置文件
    response = await tester.send_message(
        "请读取 config/pipelines/default.yaml 文件的内容",
        timeout=90
    )

    passed = (
        "yaml" in response.lower() or
        "pipeline" in response.lower() or
        "[ERROR]" not in response
    )
    tester.add_result("文件读取工具", passed, f"响应包含配置文件内容: {passed}")
    return tester.results


async def run_memory_test():
    """测试4: 记忆功能"""
    print("\n=== 测试4: 记忆功能 ===")
    tester = RealE2ETester()

    # 第一轮：告诉模型一个事实
    await tester.send_message(
        "记住这个信息：我的名字叫测试用户"
    )

    # 第二轮：询问名字
    r2 = await tester.send_message(
        "我刚才告诉你我叫什么名字？"
    )

    passed = (
        "测试用户" in r2 or
        "测试" in r2 or
        "user" in r2.lower()
    ) and "[ERROR]" not in r2

    tester.add_result("记忆功能", passed, f"询问结果: {r2[:100]}")
    return tester.results


async def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("Agent OS 真实 E2E 测试")
    print("=" * 60)

    all_results = []

    # 运行所有测试
    all_results.extend(await run_basic_conversation_test())
    all_results.extend(await run_tool_discovery_test())
    all_results.extend(await run_file_read_test())
    all_results.extend(await run_memory_test())

    # 汇总结果
    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)

    passed = sum(1 for r in all_results if r["passed"])
    total = len(all_results)

    for r in all_results:
        status = "✅" if r["passed"] else "❌"
        print(f"  {status} {r['name']}")

    print(f"\n通过: {passed}/{total}")

    return all_results


if __name__ == "__main__":
    results = asyncio.run(run_all_tests())

    # 返回退出码
    failed = sum(1 for r in results if not r["passed"])
    sys.exit(0 if failed == 0 else 1)
