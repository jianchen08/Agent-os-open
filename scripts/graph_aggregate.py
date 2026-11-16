"""全景图聚合层：增量聚合 + 落盘 graph.js + 校验红绿（三合一入口）。

方案 §4.4：本地一条命令三合一——增量聚合 → 落盘 ``docs/graph/graph.js``
（gitignored 派生物，``window.GRAPH_DATA = {...}`` 赋值形态，file:// 下
viewer.html 直接可开）→ 校验红绿。校验与出图是**同一遍聚合**，数据文件
是校验的顺手副产物，不存在"只校验不出图"的独立入口。LLM 零参与。

增量口径：state 文件（gitignored）按**源文件内容 hash** 缓存单文件事实；
hash 未变的文件直接复用缓存事实，变更/新增文件重提取，已删除文件随
entries 重建自然摘除。方案原文写"git diff 变更集"，此处等价替换为 hash
对比——门禁跑在工作区，hash 对比同时覆盖已提交与未提交改动（git diff
漏未提交态），且省去一次 git 子进程；state 仍记录 HEAD 供诊断。
每条边由其源文件单向导出，按文件粒度替换即完整，无跨文件全局推导。

纯标准库（CI 只装 CPython）；本仓脚本门禁纪律见 scripts/scripts.md。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import graph_check
import graph_extract
import graph_model as gm

_DEFAULT_ROOT = Path(__file__).resolve().parents[1]

#: 双链关系缺省词（开放词表；缺省取中性关系，viewer 按 k 着色）。
_DEFAULT_RELATION = "依据"


# ── 源发现与提取 ─────────────────────────────────────────────────────


def _anchored_rglob(root: Path, pattern: str) -> list[str]:
    """按模式的**字面前缀目录**锚定后递归（模式按仓根相对解释）。

    ``Path.rglob`` 对仓根调用的实际代价是「遍历整个仓库树再去匹配」——工作副本
    里 ``ws/``（任务工作空间）、``node_modules/``、``release/``、``kernel/target-*``
    体量巨大，实测 120s 不返回（2026-10-28 本地跑 CI 卡在
    ``test_real_repo_aggregates_clean`` 的根因）。本函数把模式在首个通配段处
    切开，只从字面前缀目录（如 ``docs/``、``skills/``、``config/agents``）起
    glob。语义差异只在「同名子树出现在别处」：``node_modules/pkg/docs/x.md``
    不再被 ``docs/**/*.md`` 收进源集合——这正是仓根相对模式的应有口径。
    """
    parts = pattern.split("/")
    split_at = next(
        (idx for idx, part in enumerate(parts) if any(ch in part for ch in "*?[")),
        None,
    )
    if split_at is None:
        # 无通配段：整体作仓根相对模式
        fixed, rest = [], parts
    else:
        fixed, rest = parts[:split_at], parts[split_at:]
    base = root.joinpath(*fixed) if fixed else root
    if not base.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in base.glob("/".join(rest)) if p.is_file())


#: 提取源排除前缀：资料池不是文档——`rp_prompt_material/` 是 164 份 RP 素材
#: （~1.4MB，零字节日志进 LLM 上下文），正文里的 ``[[thread]]``/``[[/thread]]``
#: 是素材模板语法而非双链，进图只会制造恒悬空红点并让数据文件膨胀。
_EXCLUDED_SOURCE_PREFIXES: tuple[str, ...] = ("docs/working/rp_prompt_material/",)


def discover_files(root: Path) -> list[str]:
    """发现全部提取源（方案 §4.2 源表 + docs 下全部 md），posix 相对路径。"""

    def _glob(pattern: str) -> list[str]:
        return sorted(p.relative_to(root).as_posix() for p in root.glob(pattern) if p.is_file())

    def _rglob(pattern: str) -> list[str]:
        return _anchored_rglob(root, pattern)

    files: set[str] = set()
    files.update(_glob("plugins/shared/*/plugin.json"))
    files.update(_glob("kernel/crates/*/Cargo.toml"))
    files.update(_rglob("config/agents/*.yaml"))
    files.update(_rglob("config/agents/*/*.yaml"))
    files.update(_rglob("config/agents/*/*/*.yaml"))
    files.update(_glob("plugins/shared/mode_*/mode.yaml"))
    files.update(_rglob("skills/**/SKILL.md"))
    files.update(_glob("config/pipelines/*.yaml"))
    files.update(_glob("config/steps/*.yaml"))
    files.update(_rglob("docs/**/*.md"))
    return sorted(rel for rel in files if not rel.startswith(_EXCLUDED_SOURCE_PREFIXES))


def _read_text(path: Path) -> str:
    # errors="replace"：仓内偶有非严格 utf-8 文档，替换坏字节保聚合不炸；
    # hash 基于替换后文本，同一文件每次替换结果一致，增量判定不受影响。
    return path.read_text(encoding="utf-8", errors="replace")


def _git_commit(root: Path) -> str:
    """HEAD 短 hash；无 git 环境（tmp/裸目录）返回空串。

    commit 仅用于数据文件底栏展示（新旧肉眼可判），获取失败不阻塞聚合。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return proc.stdout.strip()


# ── 装配：facts 列表 → data + metas + dangling ───────────────────────


def assemble(
    facts_list: list[gm.SourceFacts],
    root: Path,
) -> tuple[dict, list[gm.DocMeta], list[str]]:
    """把单文件事实装配成图数据（纯内存遍历，毫秒级）。

    返回 ``(data, metas, dangling)``：data 为 viewer 契约三键形态；
    metas 供校验层执法；dangling 为未解析双链（校验三件之一）。
    """
    nodes: dict[str, gm.Node] = {
        gm.PROJECT_NODE_ID: gm.Node(id=gm.PROJECT_NODE_ID, type="project", label=gm.PROJECT_NODE_LABEL),
    }
    metas: list[gm.DocMeta] = []
    path_index: dict[str, str] = {}  # 源文件 rel → 主节点 id
    service_index: dict[str, str] = {}  # 服务名（含首段别名）→ plugin 节点 id
    tool_index: dict[str, str] = {}  # 工具名 → plugin 节点 id
    crate_ids: set[str] = set()

    for facts in facts_list:
        if facts.meta is not None:
            metas.append(facts.meta)
        if not facts.nodes:
            continue
        node = facts.nodes[0]
        if node.id in nodes:
            continue  # id 冲突（如重复 doc_id）：节点先到先得，冲突由 check 执法
        nodes[node.id] = node
        if facts.kind == gm.KIND_CRATE:
            crate_ids.add(node.id)
        if facts.rel not in path_index:
            path_index[facts.rel] = node.id
        for name in facts.provides_services:
            service_index.setdefault(name, node.id)
            service_index.setdefault(name.split(".")[0], node.id)
        for name in facts.provides_tools:
            tool_index.setdefault(name, node.id)

    edges: list[gm.Edge] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def _add(edge: gm.Edge) -> None:
        key = (edge.s, edge.t, edge.k)
        if key not in seen_edges and edge.s != edge.t:
            seen_edges.add(key)
            edges.append(edge)

    # 归属边（代码半边机械生成 → viewer 架构视图树骨干；文档半边靠正文
    # [[proj|归属]] 双链，渐进补齐，不机械生成——v3.7 归属=逻辑关系裁定）
    for node in nodes.values():
        if node.type in gm.CODE_KINDS:
            _add(gm.Edge(s=node.id, t=gm.PROJECT_NODE_ID, k=gm.REL_BELONGS_TO))

    for facts in facts_list:
        # 依赖边：requires_services / Cargo [dependencies] / tool_ids。
        # 解析不到提供方不生成边——服务/工具可能由内核原生提供，且依赖边
        # 不在校验三件之内（§4.4：校验 = schema / 双链悬空 / status）。
        for need in facts.services:
            target = service_index.get(need.service) or service_index.get(need.service.split(".")[0])
            if target:
                _add(gm.Edge(s=need.source_id, t=target, k=gm.REL_DEPENDS_ON, note=need.service))
        for dep in facts.crate_deps:
            target = f"crate:{dep.dep}"
            if target in crate_ids:
                _add(gm.Edge(s=dep.source_id, t=target, k=gm.REL_DEPENDS_ON))
        for tool_need in facts.tool_needs:
            target = tool_index.get(tool_need.tool_id)
            if target:
                _add(gm.Edge(s=tool_need.source_id, t=target, k=gm.REL_DEPENDS_ON, note=tool_need.tool_id))

    dangling: list[str] = []
    for facts in facts_list:
        source_rel = facts.rel
        for link in facts.links:
            target_id = _resolve_link(link.target, nodes, path_index, root)
            if target_id is None:
                if gm.DOC_ID_RE.match(link.target) or gm.looks_like_reference(link.target):
                    dangling.append(f"{source_rel}: 双链目标 {link.target} 未解析（{link.relation or '无关系'}）")
                continue  # 纯中文占位 = 正文写法示例，忽略不报
            _add(
                gm.Edge(
                    s=link.source_id,
                    t=target_id,
                    k=link.relation or _DEFAULT_RELATION,
                    note=link.note,
                )
            )

    data = {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "commit": _git_commit(root),
        },
        "nodes": [asdict(n) for n in _sorted_nodes(nodes)],
        "edges": [asdict(e) for e in sorted(edges, key=lambda e: (e.s, e.t, e.k, e.note))],
    }
    return data, metas, dangling


def _sorted_nodes(nodes: dict[str, gm.Node]) -> list[gm.Node]:
    """按 (type, id) 排序——增量路径与全量重建产出同一顺序（幂等）。"""
    return sorted(nodes.values(), key=lambda n: (n.type, n.id))


def _resolve_link(
    target: str,
    nodes: dict[str, gm.Node],
    path_index: dict[str, str],
    root: Path,
) -> str | None:
    """双链目标解析：doc_id → 源路径 → 仓库文件（落 file 节点）→ None。

    命中仓库文件时把 file 节点写入 ``nodes``（id = 仓库相对路径，viewer
    DEMO 同形态：节点带 path 字段按需渲染）。
    """
    if target in nodes:
        return target
    hit = path_index.get(target)
    if hit:
        return hit
    if (root / target).is_file():
        if target not in nodes:
            nodes[target] = gm.Node(
                id=target,
                type="file",
                label=target.rsplit("/", 1)[-1],
                path=target,
            )
        return target
    return None


def build_graph(root: Path) -> tuple[dict, list[gm.DocMeta], list[str]]:
    """全量便捷组合：发现 → 提取 → 装配（测试与手动调用用）。"""
    facts_list = [graph_extract.extract_source(rel, _read_text(root / rel)) for rel in discover_files(root)]
    return assemble(facts_list, root)


# ── 增量 state 序列化 ────────────────────────────────────────────────


def _facts_to_dict(facts: gm.SourceFacts) -> dict:
    return {
        "rel": facts.rel,
        "kind": facts.kind,
        "nodes": [asdict(n) for n in facts.nodes],
        "links": [asdict(link) for link in facts.links],
        "services": [asdict(s) for s in facts.services],
        "crate_deps": [asdict(d) for d in facts.crate_deps],
        "tool_needs": [asdict(t) for t in facts.tool_needs],
        "provides_services": list(facts.provides_services),
        "provides_tools": list(facts.provides_tools),
        "meta": asdict(facts.meta) if facts.meta is not None else None,
    }


def _facts_from_dict(raw: dict) -> gm.SourceFacts:
    meta_raw = raw.get("meta")
    return gm.SourceFacts(
        rel=raw["rel"],
        kind=raw["kind"],
        nodes=[gm.Node(**n) for n in raw["nodes"]],
        links=[gm.LinkIntent(**link) for link in raw["links"]],
        services=[gm.ServiceNeed(**s) for s in raw["services"]],
        crate_deps=[gm.CrateDep(**d) for d in raw["crate_deps"]],
        tool_needs=[gm.ToolNeed(**t) for t in raw["tool_needs"]],
        provides_services=list(raw.get("provides_services") or []),
        provides_tools=list(raw.get("provides_tools") or []),
        meta=gm.DocMeta(**meta_raw) if meta_raw else None,
    )


def _load_state(state_path: Path) -> dict:
    """读增量 state；缺失/损坏一律视为无缓存（全量重建，秒级）。"""
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ── 三合一入口 ────────────────────────────────────────────────────────


def aggregate(root: Path, out_path: Path, state_path: Path) -> int:
    """增量聚合 → 落盘 graph.js → 校验红绿。返回进程退出码（0 绿 / 1 红）。

    校验红时数据文件仍落盘（副产物语义：图永远有新鲜数据，红绿由退出码
    表达，供 run_gates / 手动跑判定）。
    """
    root = Path(root)
    out_path = Path(out_path)
    state_path = Path(state_path)

    cached = _load_state(state_path).get("files", {})
    entries: dict[str, dict] = {}
    facts_list: list[gm.SourceFacts] = []
    extracted = 0
    for rel in discover_files(root):
        content = _read_text(root / rel)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        entry = cached.get(rel)
        if entry and entry.get("hash") == digest:
            facts_list.append(_facts_from_dict(entry["facts"]))
        else:
            facts = graph_extract.extract_source(rel, content)
            facts_list.append(facts)
            entry = {"hash": digest, "facts": _facts_to_dict(facts)}
            extracted += 1
        entries[rel] = entry

    data, metas, dangling = assemble(facts_list, root)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "window.GRAPH_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n"
    out_path.write_text(payload, encoding="utf-8")

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {"head": _git_commit(root), "files": entries},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    findings = graph_check.check_graph(data, metas, dangling)
    if findings:
        print(f"graph-check: {len(findings)} 个问题", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1

    print(
        f"graph: {len(data['nodes'])} 节点 / {len(data['edges'])} 边 （重提取 {extracted}/{len(entries)}）→ {out_path}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="项目全景图三合一：增量聚合 → 落盘 graph.js → 校验红绿")
    parser.add_argument("--root", default=str(_DEFAULT_ROOT), help="仓库根目录")
    parser.add_argument("--out", default="", help="graph.js 输出路径（默认 docs/graph/graph.js）")
    parser.add_argument("--state", default="", help="增量 state 路径（默认 docs/graph/.graph_state.json）")
    ns = parser.parse_args()
    root = Path(ns.root)
    out = Path(ns.out) if ns.out else root / "docs" / "graph" / "graph.js"
    state = Path(ns.state) if ns.state else root / "docs" / "graph" / ".graph_state.json"
    return aggregate(root, out, state)


if __name__ == "__main__":
    sys.exit(main())
