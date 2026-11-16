"""项目全景图数据模型：节点/边/意图 + frontmatter 与正文双链解析原语。

真值来源（三处，改动须同步）：

- ``docs/working/重要设计/AI辅助编程体系落地AgentOS方案_20261022.md`` §4.2/§4.3
  —— 文档半边 = frontmatter 三字段（doc_id/type/status）+ 正文双链三段式
  ``[[目标|关系|逻辑说明]]``；代码半边 = 清单文件机械提取（永不手写）。
- ``docs/schemas/文档类型规范.md`` §一/§二/§六 —— 类型枚举、状态枚举、
  type-status 合法矩阵（机械执法项由 ``scripts/graph_check.py`` 承担）。
- ``docs/graph/viewer.html`` —— 渲染侧数据契约：``window.GRAPH_DATA`` 的
  ``{meta, nodes, edges}`` 三键与节点/边字段名。

本模块只放**无副作用**的原语与数据类；不读文件、不写文件、不做校验判定。
校验判定在 ``graph_check.py``，聚合落盘在 ``graph_aggregate.py``。

本仓脚本跑在纯标准库环境（CI 的 traceability-gate job 只装 CPython），
因此 frontmatter/YAML 一律用手写最小解析器，不引入 PyYAML 依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── 枚举与词表（真值：文档类型规范 §二 / §一；viewer.html TYPES + KIND_TOKEN）──

#: 追溯链上的六种文档类型（``rule`` 已于 v2.1 移出枚举——规则配置数据非文档类型）。
DOC_TYPES: frozenset[str] = frozenset({"requirement", "plan", "adr", "design", "report", "ledger"})

#: 文档状态四值（文档类型规范 §四 frontmatter 三字段）。
DOC_STATUSES: frozenset[str] = frozenset({"draft", "active", "superseded", "archived"})

#: frontmatter 允许的字段集合——仅三字段（domain/supersedes/truth_for 已裁撤）。
FRONTMATTER_FIELDS: frozenset[str] = frozenset({"doc_id", "type", "status"})

#: doc_id 形态：``YYYYMMDD-小写连字符串``（仓内既有三例均遵此形态）。
DOC_ID_RE = re.compile(r"^\d{8}-[a-z0-9]+(?:-[a-z0-9]+)*$")

#: 正文双链三段式 ``[[目标|关系|逻辑说明]]``（关系词表开放：归属/签发/验证/真值/
#: 依赖/验收/取代/依据/上下游，viewer 按 KIND_TOKEN 映射到色板）。
WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]|]*))?(?:\|([^\[\]]*))?\]\]")

#: 代码半边节点类型（方案 §4.2 提取源表）。
KIND_PLUGIN = "plugin"
KIND_CRATE = "crate"
KIND_AGENT = "agent"
KIND_MODE = "mode"
KIND_SKILL = "skill"
KIND_PIPELINE = "pipeline"
KIND_DOC = "doc"

CODE_KINDS: frozenset[str] = frozenset({KIND_PLUGIN, KIND_CRATE, KIND_AGENT, KIND_MODE, KIND_SKILL, KIND_PIPELINE})

#: 归属关系名（方案 §4.2 层级边在 v3.7 统一为逻辑关系边；viewer 视其为树骨干）。
REL_BELONGS_TO = "归属"
#: 取代关系名（文档类型规范 §六：superseded 必有 supersedes 指向）。
REL_SUPERSEDES = "取代"
#: 依赖关系名（方案 §4.2：depends_on 机械提取）。
REL_DEPENDS_ON = "依赖"

#: 项目根节点 id。
#:
#: 取值 ``proj`` 不是随意选择——已入库的渲染规定 ``docs/graph/viewer.html``
#: 三处硬编码了它（``parent==='proj'`` 取消复合容器、``parentOf.get(e.s)!=='proj'``
#: 决定直属归属边是否绘制、``id!=='proj'`` 决定折叠豁免）。故项目根 id 是**渲染
#: 契约**而非内部实现细节，改动须同步 viewer。
PROJECT_NODE_ID = "proj"

#: 项目根节点标签（viewer 侧 "项目" 类型的中文名由 TYPES 映射给出，此处仅是显示名）。
PROJECT_NODE_LABEL = "灵汐 AgentOS"


@dataclass(frozen=True)
class Node:
    """图节点（渲染侧契约见 viewer.html ``buildElements``）。"""

    id: str
    type: str
    label: str
    path: str = ""
    status: str = ""
    content: str = ""
    owner: str = ""


@dataclass(frozen=True)
class Edge:
    """图边（``s``=起点，``t``=终点；viewer 双向建邻接，``归属`` 边另有父子语义）。"""

    s: str
    t: str
    k: str
    note: str = ""
    owner: str = ""


@dataclass(frozen=True)
class LinkIntent:
    """文档半边的双链意图（本地事实：目标未解析，边在全局装配阶段生成）。

    目标可指向文档（doc_id）或代码/文件（仓库相对路径），由别名表解析。
    """

    source_id: str
    target: str
    relation: str
    note: str
    owner: str = ""


@dataclass(frozen=True)
class ServiceNeed:
    """插件声明的服务依赖（``requires_services``）——解析为对提供方插件的依赖边。"""

    source_id: str
    service: str
    owner: str = ""


@dataclass(frozen=True)
class CrateDep:
    """Rust crate 依赖声明（Cargo.toml ``[dependencies]``）——解析为 crate 间依赖边。"""

    source_id: str
    dep: str
    owner: str = ""


@dataclass(frozen=True)
class ToolNeed:
    """agent 声明的工具引用（``tool_ids``）——解析为对提供方插件的依赖边。"""

    source_id: str
    tool_id: str
    owner: str = ""


@dataclass
class SourceFacts:
    """单个源文件的本地事实（可增量缓存：换文件即换事实，无跨文件推导）。"""

    rel: str
    kind: str
    nodes: list[Node] = field(default_factory=list)
    links: list[LinkIntent] = field(default_factory=list)
    services: list[ServiceNeed] = field(default_factory=list)
    crate_deps: list[CrateDep] = field(default_factory=list)
    tool_needs: list[ToolNeed] = field(default_factory=list)
    #: 本文件提供的服务/工具名（仅 plugin 源填充，取自 capabilities.services/tools
    #: 的 name）——装配阶段供 requires_services / tool_ids 解析提供方。
    provides_services: list[str] = field(default_factory=list)
    provides_tools: list[str] = field(default_factory=list)
    meta: DocMeta | None = None


@dataclass(frozen=True)
class DocMeta:
    """文档半边 frontmatter 元数据（校验面输入）。"""

    doc_id: str
    type: str
    status: str
    path: str
    extra_fields: tuple[str, ...] = ()


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """解析文档头部 frontmatter，返回 ``(字段字典, 正文)``。

    只解析顶层 ``key: value`` 标量（仓内三字段形态），其余行忽略；无 frontmatter
    时字段字典为空。刻意不做完整 YAML 解析——CI 侧无 PyYAML 依赖，且本仓 frontmatter
    形态被文档类型规范 §一 锁定为三标量字段。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields: dict[str, str] = {}
    body_start = len(lines)
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            body_start = index + 1
            break
        if not line.strip() or line.startswith((" ", "\t", "#")):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        fields[key.strip()] = value.strip().strip('"').strip("'")
    else:
        return {}, text
    return fields, "\n".join(lines[body_start:])


def strip_code(text: str) -> str:
    """去掉围栏代码块与行内代码（作者写在代码里的示例双链不是引用）。

    方案 §4.2 的示例 ``[[目标|关系|逻辑说明]]`` 在文档类型规范里就写在行内代码中，
    若不剔除会被误当成真实引用而判成悬空。
    """
    out_lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        out_lines.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(out_lines)


def iter_wikilinks(text: str) -> list[tuple[str, str, str]]:
    """抽取正文双链，返回 ``[(目标, 关系, 说明), ...]``。

    关系缺省为空串（调用方按开放词表处理）；代码块与行内代码内的写法不算引用。
    """
    found: list[tuple[str, str, str]] = []
    for match in WIKILINK_RE.finditer(strip_code(text)):
        target = match.group(1).strip()
        relation = (match.group(2) or "").strip()
        note = (match.group(3) or "").strip()
        if target:
            found.append((target, relation, note))
    return found


def looks_like_reference(target: str) -> bool:
    """判断双链目标是否"形似真实引用"（用于悬空判定，避免误伤示例占位）。

    形似引用的目标：doc_id 形态（``20261022-xxx``）或带路径分隔符/代码文件后缀。
    纯中文占位（``[[目标]]``、``[[路径]]``、``[[doc_id]]``）不算引用——它们是
    文档正文里的写法示例，方案 §4.2 正是这样写的。
    """
    if DOC_ID_RE.match(target):
        return True
    if "/" in target or "\\" in target:
        return True
    return bool(re.search(r"\.(?:py|rs|ts|tsx|yaml|yml|json|toml|md|html|js|mjs|txt)$", target))


def parse_simple_yaml(text: str) -> dict[str, str]:
    """解析 agent/mode/pipeline 清单的顶层标量字段（最小 YAML 子集）。

    只取顶层 ``key: value``（值可含 ``#`` 注释前的部分），供 id/name 抽取使用；
    列表字段（``tool_ids``）由 :func:`parse_simple_yaml_list` 单独取。
    """
    out: dict[str, str] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line or line[0] in " \t#-":
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        if not value and _block_follows(lines, index + 1):
            continue
        out[key.strip()] = value.strip('"').strip("'")
    return out


def _block_follows(lines: list[str], start: int) -> bool:
    """后续首个非空行是否缩进（即当前空值键是列表/子映射的父键）。"""
    for line in lines[start:]:
        if not line.strip():
            continue
        return line[0] in " \t"
    return False


def parse_simple_yaml_list(text: str, key: str) -> list[str]:
    """解析 YAML 顶层列表字段（``key:`` 换行后 ``- item`` 序列）。"""
    items: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != f"{key}:":
            continue
        for follower in lines[index + 1 :]:
            stripped = follower.strip()
            if not stripped:
                continue
            if not stripped.startswith("- "):
                break
            items.append(stripped[2:].strip().strip('"').strip("'"))
        break
    return items
