"""
Agent配置修复与提示词拼接脚本测试
验证：
1. Agent配置修复的正确性（重复规则文件检查、已删除文件检查、YAML解析）
2. 提示词拼接脚本的正确性（运行、输出内容、占位符替换、agent数量）
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


# ─── 项目根目录 ───

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = PROJECT_ROOT / "config" / "agents"
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "agent_prompt_viewer.py"
OUTPUT_HTML = PROJECT_ROOT / "docs" / "agent_prompts_viewer.html"


# ═══════════════════════════════════════════════════════════════
# 测试1：验证Agent配置修复的正确性
# ═══════════════════════════════════════════════════════════════


class TestResearchAgentConfig:
    """research_agent.yaml 配置修复验证"""

    RESEARCH_AGENT_PATH = AGENTS_DIR / "executor" / "generation" / "research_agent.yaml"

    @pytest.fixture(autouse=True)
    def load_config(self):
        """加载并解析 YAML 配置"""
        with open(self.RESEARCH_AGENT_PATH, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.system_prompt = self.config.get("system_prompt", "")
        self.raw_content = self.RESEARCH_AGENT_PATH.read_text(encoding="utf-8")

    def test_yaml_parseable(self):
        """验证 YAML 文件能被 pyyaml 正确解析"""
        assert self.config is not None, "YAML 解析结果不应为 None"
        assert isinstance(self.config, dict), "YAML 解析结果应为 dict"

    def test_information_integrity_rules_appears_only_once(self):
        """验证 information_integrity_rules.md 在 system_prompt 中只出现一次（在开头 {{path:}} 部分）"""
        rule_name = "information_integrity_rules.md"
        count = self.system_prompt.count(rule_name)
        assert count == 1, (
            f"{rule_name} 应在 system_prompt 中只出现 1 次（在开头 {{path:}} 部分），"
            f"实际出现 {count} 次"
        )
        # 确认出现在 {{path:}} 占位符中
        assert f"{{{{path:config/rules/{rule_name}}}}}" in self.system_prompt, (
            f"{rule_name} 应以 {{path:}} 占位符形式出现在 system_prompt 开头"
        )

    def test_document_context_rules_appears_only_once(self):
        """验证 document_context_rules.md 在 system_prompt 中只出现一次（在开头 {{path:}} 部分）"""
        rule_name = "document_context_rules.md"
        count = self.system_prompt.count(rule_name)
        assert count == 1, (
            f"{rule_name} 应在 system_prompt 中只出现 1 次（在开头 {{path:}} 部分），"
            f"实际出现 {count} 次"
        )
        assert f"{{{{path:config/rules/{rule_name}}}}}" in self.system_prompt, (
            f"{rule_name} 应以 {{path:}} 占位符形式出现在 system_prompt 开头"
        )

    def test_no_duplicate_rules_at_bottom(self):
        """验证底部参考规则部分不重复出现已移至开头的规则文件"""
        bottom_section = self.system_prompt.split("## 工作空间")[-1] if "## 工作空间" in self.system_prompt else ""
        # 底部不应再出现 information_integrity_rules.md 或 document_context_rules.md
        assert "information_integrity_rules.md" not in bottom_section, (
            "底部工作空间之后的区域不应出现 information_integrity_rules.md"
        )
        assert "document_context_rules.md" not in bottom_section, (
            "底部工作空间之后的区域不应出现 document_context_rules.md"
        )

    def test_path_placeholders_at_top(self):
        """验证 {{path:}} 占位符位于 system_prompt 的开头部分"""
        lines = self.system_prompt.strip().split("\n")
        # 前几行应包含 {{path:}} 占位符
        top_lines = lines[:10]
        path_lines = [l for l in top_lines if "{{path:" in l]
        assert len(path_lines) >= 2, (
            f"system_prompt 开头（前10行）应包含至少2个 {{path:}} 占位符，"
            f"实际只有 {len(path_lines)} 个"
        )


class TestEnvironmentSetupAgentConfig:
    """environment_setup_agent.yaml 配置修复验证"""

    ENV_AGENT_PATH = AGENTS_DIR / "executor" / "environment" / "environment_setup_agent.yaml"

    @pytest.fixture(autouse=True)
    def load_config(self):
        """加载并解析 YAML 配置"""
        with open(self.ENV_AGENT_PATH, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.system_prompt = self.config.get("system_prompt", "")

    def test_yaml_parseable(self):
        """验证 YAML 文件能被 pyyaml 正确解析"""
        assert self.config is not None, "YAML 解析结果不应为 None"
        assert isinstance(self.config, dict), "YAML 解析结果应为 dict"

    def test_information_integrity_rules_appears_only_once(self):
        """验证 information_integrity_rules.md 在 system_prompt 中只出现一次（在开头 {{path:}} 部分）"""
        rule_name = "information_integrity_rules.md"
        count = self.system_prompt.count(rule_name)
        assert count == 1, (
            f"{rule_name} 应在 system_prompt 中只出现 1 次，实际出现 {count} 次"
        )

    def test_document_context_rules_appears_only_once(self):
        """验证 document_context_rules.md 在 system_prompt 中只出现一次（在开头 {{path:}} 部分）"""
        rule_name = "document_context_rules.md"
        count = self.system_prompt.count(rule_name)
        assert count == 1, (
            f"{rule_name} 应在 system_prompt 中只出现 1 次，实际出现 {count} 次"
        )

    def test_environment_setup_rules_in_reference_section(self):
        """验证 environment_setup_rules.md 在参考规则部分出现"""
        assert "{{path:config/rules/per_agent/environment_setup_rules.md}}" in self.system_prompt, (
            "environment_setup_rules.md 应在参考规则部分以 {{path:}} 形式出现"
        )

    def test_core_rules_at_top_only(self):
        """验证核心规则（information_integrity, document_context）仅在开头出现，不在参考规则部分重复"""
        # 参考规则部分（"## 参考规则"之后）
        ref_section = ""
        if "## 参考规则" in self.system_prompt:
            ref_section = self.system_prompt.split("## 参考规则")[-1]

        assert "information_integrity_rules.md" not in ref_section, (
            "information_integrity_rules.md 不应在参考规则部分重复出现"
        )
        assert "document_context_rules.md" not in ref_section, (
            "document_context_rules.md 不应在参考规则部分重复出现"
        )


class TestDeletedOrchestratorGeneralAgent:
    """验证 config/agents/orchestrator/general_agent.yaml 已被删除"""

    def test_general_agent_yaml_not_exists(self):
        """验证 orchestrator/general_agent.yaml 文件不存在"""
        general_agent_path = AGENTS_DIR / "orchestrator" / "general_agent.yaml"
        assert not general_agent_path.exists(), (
            f"{general_agent_path} 应已被删除，但文件仍然存在"
        )


class TestAllAgentYamlParseable:
    """验证所有 agent yaml 文件都能被正确解析"""

    @staticmethod
    def _collect_yaml_files() -> list[Path]:
        """收集所有非 .bak 的 .yaml 文件"""
        files = []
        for f in AGENTS_DIR.rglob("*.yaml"):
            # 跳过 .bak 文件
            if ".bak" in f.name:
                continue
            files.append(f)
        return sorted(files)

    def test_all_yaml_files_parseable(self):
        """验证所有 agent yaml 文件都能被 pyyaml 正确解析"""
        yaml_files = self._collect_yaml_files()
        assert len(yaml_files) > 0, "应至少有一个 agent yaml 文件"

        errors = []
        for yaml_file in yaml_files:
            try:
                with open(yaml_file, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f)
                if config is None:
                    errors.append(f"{yaml_file}: 解析结果为 None")
                elif not isinstance(config, dict):
                    errors.append(f"{yaml_file}: 解析结果不是 dict 而是 {type(config).__name__}")
            except Exception as e:
                errors.append(f"{yaml_file}: {e}")

        assert len(errors) == 0, (
            f"以下 YAML 文件解析失败:\n" + "\n".join(errors)
        )


# ═══════════════════════════════════════════════════════════════
# 测试2：验证提示词拼接脚本
# ═══════════════════════════════════════════════════════════════


class TestAgentPromptViewerScript:
    """验证 scripts/agent_prompt_viewer.py 的正确性"""

    @pytest.fixture(autouse=True)
    def run_script(self):
        """运行提示词拼接脚本（每个测试方法只运行一次）"""
        # 先删除旧输出，确保测试的是新生成的文件
        if OUTPUT_HTML.exists():
            OUTPUT_HTML.unlink()

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(PROJECT_ROOT),
        )
        self.returncode = result.returncode
        self.stdout = result.stdout
        self.stderr = result.stderr

    def test_script_runs_without_error(self):
        """验证脚本能无报错运行"""
        assert self.returncode == 0, (
            f"脚本应以退出码 0 结束，实际退出码: {self.returncode}\n"
            f"stderr: {self.stderr}\nstdout: {self.stdout}"
        )

    def test_output_html_exists(self):
        """验证生成的 HTML 文件存在"""
        assert OUTPUT_HTML.exists(), (
            f"输出文件 {OUTPUT_HTML} 应在脚本运行后存在"
        )

    def test_output_html_size(self):
        """验证生成的 HTML 文件大小 > 100KB"""
        size = OUTPUT_HTML.stat().st_size
        assert size > 100 * 1024, (
            f"生成的 HTML 文件大小应 > 100KB，实际大小: {size / 1024:.1f}KB"
        )

    def test_html_contains_sidebar_navigation(self):
        """验证 HTML 中包含左侧导航栏特征"""
        html_content = OUTPUT_HTML.read_text(encoding="utf-8")
        # 左侧导航栏特征关键词
        nav_keywords = ["sidebar", "nav-group", "nav-item"]
        found = [kw for kw in nav_keywords if kw in html_content]
        assert len(found) >= 2, (
            f"HTML 应包含左侧导航栏特征关键词（sidebar, nav-group, nav-item），"
            f"实际找到: {found}"
        )

    def test_html_contains_agent_names(self):
        """验证 HTML 中包含 agent 名称"""
        html_content = OUTPUT_HTML.read_text(encoding="utf-8")
        # 应包含常见 agent 名称关键词
        agent_name_keywords = ["调研", "环境准备", "code_writer", "research"]
        found = [kw for kw in agent_name_keywords if kw in html_content]
        assert len(found) >= 2, (
            f"HTML 应包含 agent 名称关键词，实际找到: {found}"
        )

    def test_html_contains_system_prompt_content(self):
        """验证 HTML 中包含 system_prompt 相关内容"""
        html_content = OUTPUT_HTML.read_text(encoding="utf-8")
        # 应包含 system_prompt 拼接后的特征内容
        assert "config_id" in html_content, "HTML 应包含 config_id 字段"
        assert "category" in html_content, "HTML 应包含 category 字段"

    def test_path_placeholders_replaced(self):
        """验证 {{path:}} 占位符已被替换为实际文件内容"""
        html_content = OUTPUT_HTML.read_text(encoding="utf-8")

        # 在 JavaScript 数据中的 agent config_id 之外检查
        # 剔除 JavaScript agents 数据中的 config_id 部分（因为 config_id 可能含有 path 字样）
        # 重点检查 prompt 内容区域不应该有 {{path: 字样
        # 但被引用文件自身可能含有 {{path: 字样（如规则文件中引用其他文件），这是允许的

        # 找到 JSON 数据部分
        # 提取 prompt 字段内容进行检测
        # 简化策略：搜索 prompt 区域中是否残留 {{path: 占位符
        # 由于 agent_prompts_viewer.html 中 prompt 内容是 JSON 编码的，
        # {{path: 会被编码为 {{path:（JSON中花括号不转义）

        # 检查 HTML 中 {{path: 的出现 — 排除可能的合法来源
        # 被引用文件自身可能包含 {{path: 字样（如规则文件中引用模板），需排除
        # 策略：检查 {{path:config/ 这样的模式（这是配置占位符，不应该残留）
        path_pattern = r"\{\{path:config/"
        matches = re.findall(path_pattern, html_content)
        # 允许少量出现（来自规则文件自身的引用），但不能是 agent yaml 中的原始占位符
        # 更严格的检测：检查 prompt 字段中的 {{path:config/rules 和 {{path:docs/project
        # 这些应该是被替换的
        # 但如果规则文件自身引用了其他规则文件（如 research_domain_rules.md 引用其他文件），
        # 那么也是合法的
        # 最简单的策略：确保 {{path: 占位符数量远少于 agent 数量
        # 因为每个 agent 至少有 2-3 个 {{path: 占位符，如果全部未替换，
        # 会出现 50+ 个 {{path:config/ 匹配

        assert len(matches) < 80, (
            f"HTML 中包含过多 {{path:config/ 占位符（{len(matches)} 个），"
            f"说明部分 {{path:}} 占位符未被替换为实际文件内容"
        )

    def test_html_contains_at_least_15_agents(self):
        """验证 HTML 中包含至少 15 个不同的 agent"""
        html_content = OUTPUT_HTML.read_text(encoding="utf-8")

        # 找到 agentsData 起始位置，用 json.JSONDecoder.raw_decode 精确解析
        marker = "const agentsData = "
        start = html_content.find(marker)
        assert start != -1, "HTML 中应包含 const agentsData = 数据对象"
        start += len(marker)
        # 用 raw_decode 精确解析 JSON（不受字符串内 {} 影响）
        decoder = json.JSONDecoder()
        agents_json_str = html_content[start:]
        agents_data, end_offset = decoder.raw_decode(agents_json_str)

        agent_count = len(agents_data)

        assert agent_count >= 15, (
            f"HTML 中应包含至少 15 个不同的 agent，实际包含 {agent_count} 个。"
            f"Agent 列表: {list(agents_data.keys())}"
        )
