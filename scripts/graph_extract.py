"""全景图提取层：按仓库相对路径分派提取器，产出单文件本地事实。

真值来源见 ``graph_pipeline.md`` 与 ``graph_model.py`` docstring（方案 §4.2）。
本模块为纯函数层：``extract_source`` 只吃文本不碰文件系统——读文件在
聚合层（graph_aggregate），因此本层可被增量缓存直接复用。

节点 id 形态（跨类加前缀防 id 冲突；doc 例外——doc_id 直作节点 id，
对方案 §4.5 ``?focus=<doc_id>`` 口径友好）：

- ``plugin:<id>`` / ``crate:<名>`` / ``agent:<stem>`` / ``mode:<名>``
- ``skill:<目录>`` / ``pipeline:<stem>`` / doc 用 doc_id 或 ``doc:<rel>``
"""

from __future__ import annotations

import json

import graph_model as gm

# 文档 content 内嵌截断上限：控制 600+ 文档的数据文件在"几 MB 级"（方案 §4.5
# 双层内容策略）。妥协归因：viewer 侧栏对超长文档只能看到前段；触发条件：
# viewer 改为按需拉取全文时移除截断。
CONTENT_MAX_CHARS = 4000

_TRUNCATE_SUFFIX = "\n…"


def extract_source(rel: str, text: str) -> gm.SourceFacts:
    """按路径形态分派提取器；非提取源返回空事实（kind 空串）。"""
    parts = rel.split("/")
    if len(parts) == 4 and parts[0] == "plugins" and parts[1] == "shared" and parts[3] == "plugin.json":
        return _extract_plugin(rel, text)
    if (
        len(parts) == 4
        and parts[0] == "plugins"
        and parts[1] == "shared"
        and parts[2].startswith("mode_")
        and parts[3] == "mode.yaml"
    ):
        return _extract_mode(rel, text)
    if len(parts) == 4 and parts[0] == "kernel" and parts[1] == "crates" and parts[3] == "Cargo.toml":
        return _extract_crate(rel, text)
    if len(parts) >= 3 and parts[0] == "config" and parts[1] == "agents" and parts[-1].endswith((".yaml", ".yml")):
        return _extract_agent(rel, text)
    if parts[0] == "skills" and parts[-1] == "SKILL.md":
        return _extract_skill(rel, text)
    if (
        parts[0] == "config"
        and len(parts) == 3
        and parts[1] in ("pipelines", "steps")
        and parts[2].endswith((".yaml", ".yml"))
    ):
        return _extract_pipeline(rel, text)
    if parts[0] == "docs" and len(parts) >= 2 and parts[-1].endswith(".md"):
        return _extract_doc(rel, text)
    return gm.SourceFacts(rel=rel, kind="")


def _extract_plugin(rel: str, text: str) -> gm.SourceFacts:
    data = json.loads(text)
    plugin_id = str(data.get("id") or rel.split("/")[2])
    node = gm.Node(
        id=f"plugin:{plugin_id}",
        type=gm.KIND_PLUGIN,
        label=str(data.get("name") or plugin_id),
        path=rel,
    )
    caps = data.get("capabilities") or {}
    services = [gm.ServiceNeed(source_id=node.id, service=str(s)) for s in data.get("requires_services") or []]
    return gm.SourceFacts(
        rel=rel,
        kind=gm.KIND_PLUGIN,
        nodes=[node],
        services=services,
        provides_services=[str(s.get("name") or "") for s in caps.get("services") or []],
        provides_tools=[str(t.get("name") or "") for t in caps.get("tools") or []],
    )


def _extract_crate(rel: str, text: str) -> gm.SourceFacts:
    name = rel.split("/")[2]
    node = gm.Node(id=f"crate:{name}", type=gm.KIND_CRATE, label=name, path=rel)
    deps = [gm.CrateDep(source_id=node.id, dep=dep) for dep in _parse_cargo_deps(text)]
    return gm.SourceFacts(rel=rel, kind=gm.KIND_CRATE, nodes=[node], crate_deps=deps)


def _parse_cargo_deps(text: str) -> list[str]:
    """取 Cargo.toml ``[dependencies]`` 段的顶层 key（含内外部依赖）。

    边在装配阶段按"是否命中 crate 节点"生成——外部 crate（serde 等）记为
    意图但连不上内部节点，属正常形态不算悬空。
    """
    deps: list[str] = []
    in_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_deps = stripped == "[dependencies]"
            continue
        if not in_deps or not stripped or stripped.startswith("#"):
            continue
        key, sep, _ = stripped.partition("=")
        if sep and key.strip():
            deps.append(key.strip())
    return deps


def _extract_agent(rel: str, text: str) -> gm.SourceFacts:
    fields = gm.parse_simple_yaml(text)
    stem = rel.rsplit("/", maxsplit=1)[-1].rsplit(".", 1)[0]
    node = gm.Node(
        id=f"agent:{stem}",
        type=gm.KIND_AGENT,
        label=fields.get("name") or stem,
        path=rel,
    )
    needs = [gm.ToolNeed(source_id=node.id, tool_id=t) for t in gm.parse_simple_yaml_list(text, "tool_ids")]
    return gm.SourceFacts(rel=rel, kind=gm.KIND_AGENT, nodes=[node], tool_needs=needs)


def _extract_mode(rel: str, text: str) -> gm.SourceFacts:
    fields = gm.parse_simple_yaml(text)
    mode_name = fields.get("mode") or rel.split("/")[2].removeprefix("mode_")
    node = gm.Node(
        id=f"mode:{mode_name}",
        type=gm.KIND_MODE,
        label=fields.get("name") or mode_name,
        path=rel,
    )
    return gm.SourceFacts(rel=rel, kind=gm.KIND_MODE, nodes=[node])


def _extract_skill(rel: str, text: str) -> gm.SourceFacts:
    skill_dir = rel.split("/")[-2]
    fields, _ = gm.parse_frontmatter(text)
    node = gm.Node(
        id=f"skill:{skill_dir}",
        type=gm.KIND_SKILL,
        label=fields.get("name") or skill_dir,
        path=rel,
    )
    return gm.SourceFacts(rel=rel, kind=gm.KIND_SKILL, nodes=[node])


def _extract_pipeline(rel: str, text: str) -> gm.SourceFacts:
    stem = rel.rsplit("/", maxsplit=1)[-1].rsplit(".", 1)[0]
    fields = gm.parse_simple_yaml(text)
    node = gm.Node(
        id=f"pipeline:{stem}",
        type=gm.KIND_PIPELINE,
        label=fields.get("name") or stem,
        path=rel,
    )
    return gm.SourceFacts(rel=rel, kind=gm.KIND_PIPELINE, nodes=[node])


def _extract_doc(rel: str, text: str) -> gm.SourceFacts:
    fields, body = gm.parse_frontmatter(text)
    doc_id = fields.get("doc_id", "")
    if doc_id:
        node_id = doc_id
        label = _first_heading(body) or doc_id
        meta = gm.DocMeta(
            doc_id=doc_id,
            type=fields.get("type", ""),
            status=fields.get("status", ""),
            path=rel,
            extra_fields=tuple(sorted(set(fields) - gm.FRONTMATTER_FIELDS)),
        )
    else:
        # 存量渐进补齐口径：无 frontmatter（或未声明 doc_id）仍入图，
        # id 落 ``doc:<rel>``；无元数据可校验，check 侧自然跳过。
        node_id = f"doc:{rel}"
        label = _first_heading(text) or rel.rsplit("/", maxsplit=1)[-1]
        meta = None
    node = gm.Node(
        id=node_id,
        type=gm.KIND_DOC,
        label=label,
        path=rel,
        status=fields.get("status", ""),
        content=_truncate(body),
    )
    links = [
        gm.LinkIntent(source_id=node_id, target=target, relation=relation, note=note)
        for target, relation, note in gm.iter_wikilinks(body)
    ]
    return gm.SourceFacts(rel=rel, kind=gm.KIND_DOC, nodes=[node], links=links, meta=meta)


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _truncate(text: str) -> str:
    if len(text) <= CONTENT_MAX_CHARS:
        return text
    return text[:CONTENT_MAX_CHARS] + _TRUNCATE_SUFFIX
