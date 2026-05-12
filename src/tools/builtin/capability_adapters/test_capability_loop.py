"""能力适配器工具设计闭环测试"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# 确保 src 目录在 sys.path 中
_src_dir = str(Path(__file__).parent.parent.parent.parent.parent / "src")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from tools.builtin.capability_adapters import (
    DesignGenerateTool,
    DesignReviewTool,
)
from tools.builtin.capability_adapters._base import CapabilityAdapterBase
from tools.builtin.capability_adapters._config import BackendConfig, CapabilityAdapterConfig


class TestDesignGenerateTool(unittest.TestCase):
    """DesignGenerateTool 测试"""

    def setUp(self):
        self.tool = DesignGenerateTool()

    def test_tool_definition(self):
        """验证工具名是 design_generate，输入 schema 有 description 必填字段"""
        defn = DesignGenerateTool.get_tool_definition()
        self.assertEqual(defn.name, "design_generate")
        self.assertIn("description", defn.input_schema["required"])

    def test_build_mcp_args_text(self):
        """验证 text 模式只设 prompt，不设 image/url"""
        args = self.tool._build_mcp_args(
            description="一个登录页面",
            input_type="text",
            output_format="react",
            style_preferences="",
        )
        self.assertEqual(args["prompt"], "一个登录页面")
        self.assertNotIn("image", args)
        self.assertNotIn("url", args)

    def test_build_mcp_args_screenshot(self):
        """验证 screenshot 模式只设 image，不设 prompt"""
        args = self.tool._build_mcp_args(
            description="/path/to/screenshot.png",
            input_type="screenshot",
            output_format="html",
            style_preferences="",
        )
        self.assertEqual(args["image"], "/path/to/screenshot.png")
        self.assertNotIn("prompt", args)

    def test_build_mcp_args_url(self):
        """验证 url 模式只设 url，不设 prompt"""
        args = self.tool._build_mcp_args(
            description="https://example.com",
            input_type="url",
            output_format="tailwind",
            style_preferences="dark theme",
        )
        self.assertEqual(args["url"], "https://example.com")
        self.assertNotIn("prompt", args)
        self.assertEqual(args["style"], "dark theme")

    def test_execute_no_backends(self):
        """所有后端 available=false 时返回 NO_BACKEND_CONFIGURED 错误"""
        fake_backends = [
            BackendConfig(name="fake1", available=False),
            BackendConfig(name="fake2", available=False),
        ]
        with patch.object(self.tool, "_get_backends", return_value=fake_backends):
            result = asyncio.run(
                self.tool.execute({"description": "test"})
            )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "NO_BACKEND_CONFIGURED")

    def test_transform_result_dict(self):
        """dict 输入正确提取 code/html/files/preview_url"""
        parsed = {
            "code": "<div>hello</div>",
            "files": [{"name": "index.html"}],
            "preview_url": "https://preview.example.com",
        }
        result = self.tool._transform_result(parsed, "test_backend", "html")
        self.assertTrue(result.success)
        data = result.output
        self.assertEqual(data["code"], "<div>hello</div>")
        self.assertEqual(len(data["files"]), 1)
        self.assertEqual(data["preview_url"], "https://preview.example.com")
        self.assertEqual(data["backend_used"], "test_backend")

    def test_transform_result_string(self):
        """string 输入直接作为 code"""
        parsed = "<html><body>Hello</body></html>"
        result = self.tool._transform_result(parsed, "test_backend", "html")
        self.assertTrue(result.success)
        data = result.output
        self.assertEqual(data["code"], "<html><body>Hello</body></html>")
        self.assertEqual(data["files"], [])
        self.assertIsNone(data["preview_url"])


class TestDesignReviewTool(unittest.TestCase):
    """DesignReviewTool 测试"""

    def setUp(self):
        self.tool = DesignReviewTool()

    def test_tool_definition(self):
        """验证工具名是 design_review"""
        defn = DesignReviewTool.get_tool_definition()
        self.assertEqual(defn.name, "design_review")

    def test_transform_results_normal(self):
        """正常审查结果（JS检测返回 issues）"""
        parsed_results = [
            None,
            "snapshot text here",
            '{"issues": [{"type":"layout","severity":"high","selector":"#main","description":"overflow"}]}',
            [],
        ]
        result = self.tool._transform_results(
            parsed_results, "test_backend", ["layout", "color"]
        )
        self.assertTrue(result.success)
        data = result.output
        self.assertGreater(len(data["issues"]), 0)
        self.assertIn("score", data)
        self.assertIn("by_severity", data)
        self.assertIn("summary", data)

    def test_transform_results_zero_issues(self):
        """无问题时 score 应为 100"""
        parsed_results = [
            None,
            "snapshot",
            '{"issues": []}',
            [],
        ]
        result = self.tool._transform_results(
            parsed_results, "test_backend", ["layout"]
        )
        self.assertTrue(result.success)
        data = result.output
        self.assertEqual(data["score"], 100)
        self.assertEqual(data["total_issues"], 0)

    def test_transform_results_empty(self):
        """空结果列表"""
        result = self.tool._transform_results(
            [], "test_backend", ["layout"]
        )
        self.assertTrue(result.success)
        data = result.output
        self.assertEqual(data["issues"], [])
        self.assertEqual(data["score"], 100)


class TestExtractMcpContent(unittest.TestCase):
    """_extract_mcp_content 测试"""

    def test_normal_response(self):
        """正常 MCP content 返回"""
        payload = json.dumps({"code": "<div>hi</div>"})
        mcp_result = {
            "content": [{"type": "text", "text": payload}],
        }
        extracted = CapabilityAdapterBase._extract_mcp_content(mcp_result)
        self.assertIsInstance(extracted, dict)
        self.assertEqual(extracted["code"], "<div>hi</div>")

    def test_error_response(self):
        """isError=true 时返回 {"error": True, "message": ...}"""
        mcp_result = {
            "isError": True,
            "content": [{"type": "text", "text": "连接超时"}],
        }
        extracted = CapabilityAdapterBase._extract_mcp_content(mcp_result)
        self.assertTrue(extracted["error"])
        self.assertIn("连接超时", extracted["message"])

    def test_empty_content(self):
        """空 content 返回原始 dict"""
        mcp_result = {"content": []}
        extracted = CapabilityAdapterBase._extract_mcp_content(mcp_result)
        self.assertIs(extracted, mcp_result)

    def test_non_dict(self):
        """非 dict 输入原样返回"""
        value = "just a string"
        extracted = CapabilityAdapterBase._extract_mcp_content(value)
        self.assertEqual(extracted, value)


class TestCapabilityAdapterConfig(unittest.TestCase):
    """配置加载测试"""

    def test_load_config(self):
        """配置文件能正确加载"""
        config = CapabilityAdapterConfig.load()
        self.assertIsInstance(config, dict)
        self.assertIn("design_generate", config)
        self.assertIn("design_review", config)

    def test_design_generate_backends(self):
        """design_generate 有至少一个可用后端（magic）"""
        config = CapabilityAdapterConfig.load()
        backends = config.get("design_generate", [])
        self.assertGreater(len(backends), 0)
        available = [b for b in backends if b.available]
        self.assertGreater(len(available), 0)


if __name__ == "__main__":
    unittest.main()
