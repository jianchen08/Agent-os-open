"""外部生态导入 IR（manifest 中间表示，纯数据）——翻译器与落地器之间的唯一通道。

front-end 翻译器（:mod:`codex_translator`）只读外部包产出 IR；back-end 落地器
（:mod:`skill_landing`）只消费 IR 写用户空间。IR 是冻结值对象：无方法、无副作用、
无 IO。

**凭据面不入 IR**：MCP 服务器只带 ``transport`` 与 ``requires_oauth`` 两个事实位，
``url`` / ``command`` / ``env`` / ``oauth`` 内容一律不进——外部包里的 token 与
端点不得经导入链回流到 LLM 摘要、日志或落库产物（MCP 配置写面 :mod:`mcp_config`
由落地器现场重读源文件，不经 IR 回流）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["AgentAsset", "AppEntry", "CommandAsset", "McpServer", "PluginIR", "SkillAsset"]


@dataclass(frozen=True)
class SkillAsset:
    """一个待落地技能。

    Attributes:
        skill_id: 落地目标目录名——SKILL.md frontmatter ``name``，缺失回落源目录名；
            合法性（``[A-Za-z0-9._-]``）由落地器在写入前校验。
        name: frontmatter ``name`` 原文（缺失为空串，如实反映声明面）。
        description: frontmatter ``description``（缺失为空串）。
        source_dir: 源技能目录绝对路径（整目录为落地单位：SKILL.md + 同目录资源）。
    """

    skill_id: str
    name: str
    description: str
    source_dir: Path


@dataclass(frozen=True)
class McpServer:
    """一个 MCP 服务器声明的事实位（不含凭据/端点，见模块 docstring）。"""

    name: str
    transport: str
    requires_oauth: bool


@dataclass(frozen=True)
class AppEntry:
    """一个宿主托管应用指针（Codex ``.app.json``；本仓无对应物，不可移植）。"""

    name: str
    app_id: str


@dataclass(frozen=True)
class AgentAsset:
    """一个子代理声明的事实位（本批不导入，只如实上报）。

    Attributes:
        name: frontmatter ``name``，缺失回落文件名。
        model: frontmatter ``model``（缺失为空串）。
        tools_count: frontmatter ``tools`` 的条目数（列表取元素数，字符串按逗号
            分隔计数，缺失为 0）——只报计数，工具名单不进 IR。
    """

    name: str
    model: str
    tools_count: int


@dataclass(frozen=True)
class CommandAsset:
    """一条命令声明的事实位（本批不导入，只如实上报）。

    Attributes:
        name: 相对 ``commands/`` 的路径去掉 ``.md`` 后缀（子目录保留 ``/`` 分隔，
            如 ``db/migrate``）。
    """

    name: str


@dataclass(frozen=True)
class PluginIR:
    """一个外部插件的完整中间表示。"""

    kind: str
    translator_version: str
    source_dir: Path
    name: str
    version: str
    description: str
    author: str
    license: str | None
    skills: tuple[SkillAsset, ...] = ()
    mcp_servers: tuple[McpServer, ...] = ()
    apps: tuple[AppEntry, ...] = ()
    agents: tuple[AgentAsset, ...] = ()
    commands: tuple[CommandAsset, ...] = ()
    hooks_declared: bool = False
    hooks_events: tuple[str, ...] = ()
    license_file: str | None = None
