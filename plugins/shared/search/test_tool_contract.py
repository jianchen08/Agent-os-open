# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""resource_search execute() 输入→输出契约测试（T1 断行为垫底 + T2/T3 服务化改造）。

锁的是服务化改造前后必须不变的**可观察契约**：resource_type 路由与
`_h/_d/_c` 条件出现语义、limit 收敛、通配查询、匹配语义、filters、
detailed 列扩展、坏文件跳过（工具面）。只断 execute 的返回结构，
不断私有方法。

- 工具面（T4 对账）：manifest 扫描保留为行源（项目根经 monkeypatch 模块级
  ``_PROJECT_ROOT`` 注入 tmp_path mini 仓库），与内核活注册表对账——在注入缝
  （``set_tool_surface_caller``）装活列表桩，桩按 ``GET /api/v1/tools`` 真实
  响应形态（``{items: [{name, description, plugin_id, category, source}],
  total}``）应答；对账键 = 工具名集合交，禁用插件工具 = 名字从活列表剔除即
  模拟。对账通道失败 = 显式失败信封（fail-closed，禁回落扫描）。
- Agent 面（T2 服务化）：``config/agents`` 文件扫描已删，数据源是
  agent_manager 聚合注册表服务（外部依赖，打桩合规）——在 capability
  调用缝（``set_agent_service_caller``）装桩，桩按 tool-executor 信封
  格式返回聚合格式条目；另有一条进程内集成走真实 agent_manager 聚合
  函数 + 仓内真实 config/agents 语料。服务失败 = 显式失败信封。
- Skill 面（T3 服务化）：``skills/*/SKILL.md`` 文件扫描已删，数据源是
  skill_manager 注册权威服务——在 capability 调用缝
  （``set_skill_service_caller``）装桩，桩按 provides 直连裸 dict 契约
  应答（list 元数据 / get 详情含正文）；另有一条进程内集成走真实
  skill_manager 枚举函数 + 仓内真实 skills/ 语料。服务失败 = 显式失败
  信封（fail-closed，禁回落扫描）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SERVER_MOD = "search_contract_test_server"

LIMIT_CAP = 20

# mini 仓有效工具全集（tool_a × 2 + tool_b × 1）——默认活列表桩的基准集
MINI_REPO_TOOLS = ["download_file", "upload_file", "bash_execute"]


def _load_tool_cls() -> Any:
    """经 server.py 装载工具类（继承其 sys.path/constants 接线）。"""
    if _SERVER_MOD in sys.modules:
        del sys.modules[_SERVER_MOD]
    spec = importlib.util.spec_from_file_location(_SERVER_MOD, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SERVER_MOD] = module
    spec.loader.exec_module(module)
    return module._load_resource_search_tool()


def _run(tool: Any, **inputs: Any) -> dict[str, Any]:
    result = asyncio.run(tool.execute(inputs))
    assert result.error is None, f"execute 意外失败: {result.error}"
    return result.output


@pytest.fixture
def rs() -> Any:
    return _load_tool_cls()()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rs: Any) -> Path:
    """mini 仓库（工具面扫描数据源）：3 工具（含 system 型/空 tools/坏 json）。

    skill 面数据源已服务化（T3），mini 仓不再承载 skills/ 目录。
    """
    (tmp_path / "plugins" / "tool_a").mkdir(parents=True)
    (tmp_path / "plugins" / "tool_b").mkdir(parents=True)
    (tmp_path / "plugins" / "sys_p").mkdir(parents=True)
    (tmp_path / "plugins" / "notool").mkdir(parents=True)
    (tmp_path / "plugins" / "brokenp").mkdir(parents=True)

    def plugin(p: str, body: str) -> None:
        (tmp_path / "plugins" / p / "plugin.json").write_text(body, encoding="utf-8")

    plugin("tool_a", json.dumps({
        "plugin_type": "tool",
        "capabilities": {"tools": [
            {"name": "download_file", "description": "download remote files", "tags": ["net"]},
            {"name": "upload_file", "description": "upload local files", "tags": ["net", "io"]},
        ]},
    }))
    plugin("tool_b", json.dumps({
        "plugin_type": "tool",
        "capabilities": {"tools": [
            {"name": "bash_execute", "description": "run shell commands", "tags": ["shell"]},
        ]},
    }))
    plugin("sys_p", json.dumps({
        "plugin_type": "system",
        "capabilities": {"tools": [{"name": "sys_tool", "description": "should not appear"}]},
    }))
    plugin("notool", json.dumps({"plugin_type": "tool", "capabilities": {}}))
    (tmp_path / "plugins" / "brokenp" / "plugin.json").write_text("{not json", encoding="utf-8")

    monkeypatch.setattr(sys.modules["resource_search_tool_impl"], "_PROJECT_ROOT", tmp_path)
    # 默认活列表桩 = mini 仓工具全集（对账交集语义下与改造前可见面一致；
    # 对账用例按需覆盖装桩）
    _install_tool_surface([_live_item(name) for name in MINI_REPO_TOOLS])
    return tmp_path


# ── 工具面活注册表桩（外部依赖 = 内核 GET /api/v1/tools，T4）─────────────────


def _live_item(
    name: str,
    *,
    description: str = "",
    plugin_id: str = "tool_a",
) -> dict[str, Any]:
    """GET /api/v1/tools 活条目（kernel routes.rs tools_handler 真实字段形态）。"""
    return {
        "name": name,
        "description": description,
        "plugin_id": plugin_id,
        "category": "search",
        "source": "builtin",
    }


def _install_tool_surface(items: list[dict[str, Any]]) -> list[int]:
    """在注入缝装活注册表桩，按端点信封 ``{items, total}`` 应答。

    返回调用次数记录列表（每次取数追加一项）供外部依赖交互断言。
    """
    calls: list[int] = []

    async def stub() -> dict[str, Any]:
        calls.append(1)
        return {"items": items, "total": len(items)}

    sys.modules["resource_search_tool_impl"].set_tool_surface_caller(stub)
    return calls


# ── Agent 面服务桩（外部依赖 = agent_manager 聚合注册表服务） ────────────────


def _agg(
    config_id: str,
    name: str,
    description: str,
    *,
    level: str = "",
    source: str = "system",
    **extra: Any,
) -> dict[str, Any]:
    """聚合格式条目（agent_manager.server._agent_list_item 同构）。"""
    item: dict[str, Any] = {
        "id": config_id,
        "config_id": config_id,
        "name": name,
        "description": description,
        "agent_type": "",
        "status": "active",
        "model": "",
        "level": level,
        "model_tier": "",
        "source": source,
    }
    item.update(extra)
    return item


def _install_agent_service(
    items: list[dict[str, Any]],
    configs: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """在 capability 调用缝装服务桩，按 tool-executor 信封格式应答。

    返回调用记录 [(tool_name, args)] 供外部依赖交互断言。
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    async def stub(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, dict(args)))
        if tool_name == "agent.list":
            data: dict[str, Any] = {
                "items": items,
                "total": len(items),
                "mode_registry": {"available": True, "error": None},
            }
        elif tool_name == "agent.get":
            cfg = (configs or {}).get(str(args.get("agent_id")))
            data = (
                {"found": True, "config": cfg}
                if cfg is not None
                else {"found": False, "config": None}
            )
        else:
            return {"success": False, "error": f"unexpected service: {tool_name}"}
        return {"success": True, "data": data}

    sys.modules["resource_search_tool_impl"].set_agent_service_caller(stub)
    return calls


@pytest.fixture
def agents(rs: Any) -> list[tuple[str, dict[str, Any]]]:
    """agent 面服务桩：4 条聚合格式条目（对应原 mini 仓 4 agent 语义，
    config_id 排序 = alpha/beta/delta/gamma）。"""
    return _install_agent_service([
        _agg("alpha_agent", "Alpha Agent", "research helper for deep work", level="L1"),
        _agg("beta_agent", "Beta Agent", "coding helper", level="L2"),
        _agg("gamma_agent", "Gamma Agent", "review helper", level="L3"),
        _agg("delta_helper", "Delta Agent", "", level=""),
    ])


def _names(result: dict[str, Any], kind: str) -> list[str]:
    key = f"{kind}_d"
    return [row[0] for row in result.get(key, [])]


# ── Skill 面服务桩（外部依赖 = skill_manager 注册权威服务） ──────────────────


def _skill_item(
    skill_id: str,
    name: str,
    description: str,
    languages: list[str] | None = None,
) -> dict[str, Any]:
    """skill.list 元数据条目（skill_manager list_skills 同构）。"""
    return {
        "id": skill_id,
        "name": name,
        "description": description,
        "languages": languages or [],
    }


def _install_skill_service(
    items: list[dict[str, Any]],
    contents: dict[str, str] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """在 capability 调用缝装 skill 服务桩，按 provides 直连裸 dict 契约应答。

    list → ``{"items": [...]}``；get → ``{"found", "skill": {..., content}}``
    （contents 缺该 id = found:false）。返回调用记录 [(method, args)] 供
    外部依赖交互断言。
    """
    calls: list[tuple[str, dict[str, Any]]] = []

    async def stub(method: str, args: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, dict(args)))
        if method == "list":
            return {"items": items}
        if method == "get":
            sid = str(args.get("skill_id"))
            body = (contents or {}).get(sid)
            if body is None:
                return {"found": False}
            meta = next((it for it in items if str(it.get("id")) == sid), {})
            return {"found": True, "skill": {**meta, "content": body}}
        return {"error": f"unexpected method: {method}"}

    sys.modules["resource_search_tool_impl"].set_skill_service_caller(stub)
    return calls


@pytest.fixture
def skills(rs: Any) -> list[tuple[str, dict[str, Any]]]:
    """skill 面服务桩：4 条元数据（对应原 mini 仓 4 有效 skill 语义；坏件
    过滤是注册表职责，桩条目全数有效）。"""
    return _install_skill_service(
        [
            _skill_item("one", "One Skill", "the first skill"),
            _skill_item("two", "Two Skill", "second skill paragraph"),
            _skill_item("scripted", "Scripted", "has scripts", languages=["python"]),
            _skill_item("plain", "Plain", "a plain skill"),
        ],
        contents={
            "one": "body of one",
            "two": "# Two Skill\n\nsecond skill paragraph",
            "scripted": "run it",
            "plain": "# Plain\n\na plain skill\n\nsupports download flows",
        },
    )


def _agent_names(result: dict[str, Any]) -> list[str]:
    """agent 行第 0 列是 config_id，显示名在第 1 列（header 契约）。"""
    return [row[1] for row in result.get("agent_d", [])]


class TestResourceTypeRouting:
    def test_agent_only_keys(self, rs: Any, agents: list) -> None:
        out = _run(rs, resource_type="agent")
        assert set(out) == {"agent_h", "agent_d", "agent_c"}
        assert out["agent_c"] == 4  # 桩条目全数有效（坏件过滤是服务侧职责）

    def test_tool_only_keys(self, rs: Any, repo: Path) -> None:
        out = _run(rs, resource_type="tool")
        # 表格式键不变 + 结构化定义键同批出口（tool_defs 族，动态面唯一依据）
        assert set(out) == {
            "tool_h", "tool_d", "tool_c",
            "tool_defs", "tool_defs_count", "tool_defs_errors",
        }
        assert out["tool_c"] == 3  # system 型/空 tools/坏 json 不出现
        # mini 仓 manifest 未声明 input_schema → 定义全数 fail-closed 拒收，
        # 表格式行仍保留（工具确实活着），错误项逐条可查
        assert out["tool_defs"] == []
        assert out["tool_defs_count"] == 0
        assert {e["reason"] for e in out["tool_defs_errors"]} == {"input_schema_missing"}

    def test_skill_only_keys(self, rs: Any, skills: list) -> None:
        out = _run(rs, resource_type="skill")
        assert set(out) == {"skill_h", "skill_d", "skill_c"}
        assert out["skill_c"] == 4  # 桩条目全数有效（坏件过滤是服务侧职责）

    def test_all_types(self, rs: Any, repo: Path, agents: list, skills: list) -> None:
        out = _run(rs, resource_type="all")
        assert {"agent_c", "tool_c", "skill_c"} <= set(out)

    def test_no_match_keys_absent(self, rs: Any, repo: Path, agents: list, skills: list) -> None:
        assert _run(rs, resource_type="all", query="zzz_nothing") == {}


class TestLimit:
    @staticmethod
    def _plant_many_tools(repo: Path, count: int) -> None:
        d = repo / "plugins" / "many"
        d.mkdir()
        (d / "plugin.json").write_text(json.dumps({
            "plugin_type": "tool",
            "capabilities": {"tools": [
                {"name": f"bulk_tool_{i:02d}", "description": f"bulk {i}"} for i in range(count)
            ]},
        }), encoding="utf-8")
        # 补种工具同批入活列表桩（对账交集语义：活 = 全部补种 + 基准集）
        _install_tool_surface(
            [_live_item(n) for n in MINI_REPO_TOOLS]
            + [_live_item(f"bulk_tool_{i:02d}", plugin_id="many") for i in range(count)]
        )

    def test_default_capped_at_20(self, rs: Any, repo: Path) -> None:
        self._plant_many_tools(repo, 25)
        out = _run(rs, resource_type="tool")
        assert out["tool_c"] == LIMIT_CAP
        assert len(out["tool_d"]) == out["tool_c"]

    @pytest.mark.parametrize("raw", [100, 0, -5, "abc", None])
    def test_fallback_inputs_all_cap(self, rs: Any, repo: Path, raw: Any) -> None:
        self._plant_many_tools(repo, 25)
        out = _run(rs, resource_type="tool", limit=raw)
        assert out["tool_c"] == LIMIT_CAP

    def test_small_limit_honored(self, rs: Any, repo: Path) -> None:
        self._plant_many_tools(repo, 25)
        out = _run(rs, resource_type="tool", limit=5)
        assert out["tool_c"] == 5


class TestWildcardAndMatch:
    @pytest.mark.parametrize("query", ["", "*", "all", "所有", "全部", "any"])
    def test_wildcards_return_everything(self, rs: Any, repo: Path, query: str) -> None:
        out = _run(rs, resource_type="tool", query=query)
        assert out["tool_c"] == 3

    def test_case_insensitive_substring(self, rs: Any, repo: Path) -> None:
        assert "download_file" in _names(_run(rs, resource_type="tool", query="DOWNLOAD"), "tool")

    def test_multi_keyword_any_hit(self, rs: Any, repo: Path) -> None:
        out = _run(rs, resource_type="tool", query="net, shell")
        names = _names(out, "tool")
        assert {"upload_file", "bash_execute", "download_file"} <= set(names)

    def test_tag_match(self, rs: Any, repo: Path) -> None:
        names = _names(_run(rs, resource_type="tool", query="io"), "tool")
        assert names == ["upload_file"]

    def test_agent_config_id_fallback(self, rs: Any, agents: list) -> None:
        # delta_helper 只在 config_id 里，name/description 均不含
        assert _agent_names(_run(rs, resource_type="agent", query="delta_helper")) == \
            ["Delta Agent"]

    def test_agent_wildcard_and_substring(self, rs: Any, agents: list) -> None:
        # 通配 = 全量；大小写不敏感子串命中 description
        assert _agent_names(_run(rs, resource_type="agent", query="*")) == \
            ["Alpha Agent", "Beta Agent", "Delta Agent", "Gamma Agent"]
        assert _agent_names(_run(rs, resource_type="agent", query="RESEARCH")) == \
            ["Alpha Agent"]

    def test_skill_body_fallback_match(self, rs: Any, skills: list) -> None:
        # plain 的 name/description/id 均不含 download，正文含（经 skill.get 按需补取）
        calls = _install_skill_service(
            [_skill_item("plain", "Plain", "a plain skill")],
            contents={"plain": "supports download flows"},
        )
        assert _names(_run(rs, resource_type="skill", query="download"), "skill") == ["Plain"]
        assert ("get", {"skill_id": "plain"}) in calls  # 正文经 get 按需补取

    def test_skill_id_fallback_match(self, rs: Any) -> None:
        # id 形态（原目录名）兜底：name 带空格不含下划线键
        _install_skill_service([_skill_item("delta_skill", "Delta Skill", "")])
        assert _names(_run(rs, resource_type="skill", query="delta_skill"), "skill") == \
            ["Delta Skill"]

    def test_skill_wildcard_zero_get_calls(self, rs: Any, skills: list) -> None:
        """通配查询全量命中，零 skill.get 取数（正文按需的性质断言）。"""
        out = _run(rs, resource_type="skill", query="*")
        assert out["skill_c"] == 4
        assert all(method == "list" for method, _ in skills)


class TestFilters:
    def test_level_mapping(self, rs: Any, agents: list) -> None:
        assert _agent_names(_run(rs, resource_type="agent", filters={"level": "system"})) == \
            ["Alpha Agent"]
        assert set(_agent_names(_run(rs, resource_type="agent", filters={"level": "user"}))) == \
            {"Beta Agent", "Gamma Agent"}
        assert _agent_names(_run(rs, resource_type="agent", filters={"level": "all"})) == \
            ["Alpha Agent", "Beta Agent", "Delta Agent", "Gamma Agent"]

    def test_category_agent(self, rs: Any) -> None:
        # 聚合条目一般无 category 字段（缺席即不命中）；带 category 的条目按值过滤
        _install_agent_service([
            _agg("alpha_agent", "Alpha Agent", "research helper", category="research"),
            _agg("beta_agent", "Beta Agent", "coding helper"),
        ])
        assert _agent_names(_run(rs, resource_type="agent", filters={"category": "research"})) \
            == ["Alpha Agent"]
        out = _run(rs, resource_type="agent", filters={"category": "no_such"})
        assert "agent_c" not in out

    def test_language_skill(self, rs: Any, skills: list) -> None:
        assert _names(_run(rs, resource_type="skill", filters={"language": "python"}), "skill") == \
            ["Scripted"]
        assert _run(rs, resource_type="skill", filters={"language": "nodejs"}) == {}

    def test_language_unknown_value_no_filter(self, rs: Any, skills: list) -> None:
        """未知 language 值不过滤（原文件扫描时代语义：ext_map 无键即放行）。"""
        assert _run(rs, resource_type="skill", filters={"language": "cobol"})["skill_c"] == 4


class TestDetailedMode:
    def test_skill_content_column(self, rs: Any, skills: list) -> None:
        out = _run(rs, resource_type="skill", mode="detailed")
        assert out["skill_h"] == ["skill_name", "skill_description", "skill_content"]
        row = dict(zip(out["skill_h"], out["skill_d"][0], strict=True))  # 排序后首个有效 skill = one
        assert "body of one" in row["skill_content"]
        # detailed 经 skill.get 按条目 id 补全正文（外部依赖交互断言）
        assert ("get", {"skill_id": "one"}) in skills

    def test_skill_get_not_found_is_empty_content(self, rs: Any) -> None:
        """skill.get found=false（列表/取数间隙被删）= 空内容呈现，不算失败。"""
        _install_skill_service([_skill_item("one", "One Skill", "the first skill")], contents={})
        out = _run(rs, resource_type="skill", mode="detailed")
        assert out["skill_h"] == ["skill_name", "skill_description", "skill_content"]
        assert out["skill_d"][0][2] == ""

    def test_agent_metrics_column(self, rs: Any) -> None:
        calls = _install_agent_service(
            [_agg("alpha_agent", "Alpha Agent", "research helper", level="L1")],
            configs={"alpha_agent": {
                "recommended_metrics": [
                    {"metric_id": "file_check", "default_params": {"path": "src"}},
                ],
            }},
        )
        out = _run(rs, resource_type="agent", mode="detailed")
        assert out["agent_h"] == [
            "config_id", "agent_name", "agent_description", "level", "source",
            "recommended_metrics",
        ]
        row = out["agent_d"][0]
        assert row[-1] == "推荐评估: file_check(path=src)"
        assert row[3] == "L1"
        assert row[4] == "system"
        # detailed 经 agent.get 按 item id 补全配置（外部依赖交互断言）
        assert ("agent.get", {"agent_id": "alpha_agent"}) in calls

    def test_detailed_agent_get_not_found_is_empty_metrics(self, rs: Any) -> None:
        """agent.get found=false（列表/取数间隙被删）= 无 metrics 呈现，不算失败。"""
        _install_agent_service(
            [_agg("alpha_agent", "Alpha Agent", "research helper", level="L1")],
            configs={},
        )
        out = _run(rs, resource_type="agent", mode="detailed")
        assert out["agent_h"][-1] != "recommended_metrics"
        assert len(out["agent_d"][0]) == 5

    def test_simple_has_no_metrics(self, rs: Any) -> None:
        _install_agent_service(
            [_agg("alpha_agent", "Alpha Agent", "research helper", level="L1")],
            configs={"alpha_agent": {"recommended_metrics": [{"metric_id": "file_check"}]}},
        )
        out = _run(rs, resource_type="agent")
        assert out["agent_h"] == [
            "config_id", "agent_name", "agent_description", "level", "source",
        ]
        assert len(out["agent_d"][0]) == 5

    def test_detailed_returns_real_input_schema_definition(
        self, rs: Any, repo: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """detailed 模式返回**含真实 input_schema** 的可执行定义项（动态面唯一依据）。

        方案 §11 批 2 判据 5：定义项必须携带 manifest 声明的真实
        ``input_schema``（不是 ``{}`` 占位、不是字符串化 schema），且
        ``output_schema``/``render`` 同源带出、``plugin_id`` 取内核可解析 id。
        """
        body: dict[str, Any] = {
            "id": "schema_rich_plugin",  # ≠ 目录名，锁「不按目录名解析」
            "plugin_type": "tool",
            "capabilities": {"tools": [{
                "name": "schema_rich_tool",
                "description": "a tool with a real contract",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                "render": {"card": "read"},
            }]},
        }
        d = repo / "plugins" / "schema_rich"
        d.mkdir()
        (d / "plugin.json").write_text(json.dumps(body), encoding="utf-8")
        _install_tool_surface([_live_item("schema_rich_tool", plugin_id="schema_rich_plugin")])

        out = _run(rs, resource_type="tool", query="schema_rich", mode="detailed")

        defs = {d_["name"]: d_ for d_ in out["tool_defs"]}
        assert "schema_rich_tool" in defs, out.get("tool_defs_errors")
        definition = defs["schema_rich_tool"]
        # 真实 schema 原样保真（性质断言：结构逐键相等，不是 {} 占位/字符串）
        assert definition["input_schema"] == body["capabilities"]["tools"][0]["input_schema"]
        assert definition["input_schema"]["required"] == ["path"]
        assert definition["input_schema"]["additionalProperties"] is False
        assert isinstance(definition["input_schema"], dict)
        assert definition["output_schema"]["properties"]["ok"]["type"] == "boolean"
        assert definition["render"]["card"] == "read"
        assert definition["plugin_id"] == "schema_rich_plugin"
        assert definition["plugin_id"] != "schema_rich", "定义不得带目录名作路由目标"
        # 表格键与定义同源：tool_schema 列是该条声明的 schema（不是空 dict 字符串）
        assert out["tool_defs_count"] == len(out["tool_defs"]) == 1
        assert out["tool_defs_errors"] == []


class TestRobustness:
    def test_tool_dedup_across_plugins(self, rs: Any, repo: Path) -> None:
        for p in ("dup1", "dup2"):
            d = repo / "plugins" / p
            d.mkdir()
            (d / "plugin.json").write_text(json.dumps({
                "plugin_type": "tool",
                "capabilities": {"tools": [{"name": "dup_tool", "description": f"dup in {p}"}]},
            }), encoding="utf-8")
        _install_tool_surface(
            [_live_item(n) for n in MINI_REPO_TOOLS] + [_live_item("dup_tool")]
        )
        assert _names(_run(rs, resource_type="tool", query="dup_tool"), "tool") == ["dup_tool"]

    def test_skill_frontmatter_and_title_fallback(self, rs: Any, skills: list) -> None:
        names = _names(_run(rs, resource_type="skill"), "skill")
        assert {"One Skill", "Two Skill"} <= set(names)

    def test_tool_simple_two_columns_without_schema(self, rs: Any, repo: Path) -> None:
        out = _run(rs, resource_type="tool")
        assert out["tool_h"] == ["tool_name", "tool_description"]
        assert all(len(r) == 2 for r in out["tool_d"])


class TestAgentServiceContract:
    """T2 新增：agent 行 level/source 列（权限语义可见）+ 服务面 limit。"""

    def test_level_source_columns_from_service(self, rs: Any, agents: list) -> None:
        out = _run(rs, resource_type="agent", query="helper")
        # helper 命中 alpha/delta（description/config_id），gamma 含 helper 于 description
        rows = {row[0]: row for row in out["agent_d"]}
        assert rows["alpha_agent"][3] == "L1"
        assert rows["alpha_agent"][4] == "system"
        assert rows["delta_helper"][3] == ""  # 服务侧没有的 level 如实置空
        assert all(row[4] == "system" for row in out["agent_d"])

    def test_mode_source_passthrough(self, rs: Any) -> None:
        """插件包条目 source=mode:<plugin_id> 原样透出（wire 键形不变）。"""
        _install_agent_service([
            _agg("alpha_agent", "Alpha Agent", "research helper", level="L1",
                 source="mode:mode_research", id="mode_research/alpha"),
        ])
        out = _run(rs, resource_type="agent", query="research")
        assert out["agent_d"][0][4] == "mode:mode_research"

    @pytest.mark.parametrize("raw", [2, 3])
    def test_limit_consumer_side(self, rs: Any, agents: list, raw: int) -> None:
        out = _run(rs, resource_type="agent", limit=raw)
        assert out["agent_c"] == raw  # 消费侧 limit 收敛，单调
        assert len(out["agent_d"]) == raw

    def test_invalid_envelope_is_failure(self, rs: Any) -> None:
        """success=true 但缺 data dict = 服务返回无效，显式失败。"""
        async def malformed(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"success": True}  # 缺 data
        sys.modules["resource_search_tool_impl"].set_agent_service_caller(malformed)
        result = asyncio.run(rs.execute({"resource_type": "agent"}))
        assert result.success is False
        assert result.error
        assert "返回无效" in result.error

    def test_data_without_items_is_empty_result(self, rs: Any) -> None:
        """data 无 items 键（降级形态）= 空结果键缺席，不算失败。"""
        async def no_items(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"success": True, "data": {"total": 0}}
        sys.modules["resource_search_tool_impl"].set_agent_service_caller(no_items)
        assert _run(rs, resource_type="agent") == {}

    def test_items_not_list_is_failure(self, rs: Any) -> None:
        """items 非列表 = 服务返回无效，显式失败。"""
        async def bad_items(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"success": True, "data": {"items": {"oops": 1}}}
        sys.modules["resource_search_tool_impl"].set_agent_service_caller(bad_items)
        result = asyncio.run(rs.execute({"resource_type": "agent"}))
        assert result.success is False
        assert result.error
        assert "非列表" in result.error


class TestSkillServiceContract:
    """T3 新增：skill 面消费侧降级形态契约（对照 agent 面同款语义）。"""

    def test_data_without_items_is_empty_result(self, rs: Any) -> None:
        """list 无 items 键（降级形态）= 空结果键缺席，不算失败。"""
        async def no_items(method: str, args: dict[str, Any]) -> dict[str, Any]:
            return {}
        sys.modules["resource_search_tool_impl"].set_skill_service_caller(no_items)
        assert _run(rs, resource_type="skill") == {}

    def test_items_without_id_and_name_skipped(self, rs: Any) -> None:
        """id/name 双缺条目跳过（服务侧坏条目不呈现）。"""
        _install_skill_service([
            _skill_item("good", "Good Skill", "fine"),
            {"id": "", "name": "", "description": "ghost"},
        ])
        out = _run(rs, resource_type="skill")
        assert _names(out, "skill") == ["Good Skill"]
        assert out["skill_c"] == 1

    def test_found_with_malformed_skill_dict_is_empty_content(self, rs: Any) -> None:
        """found=true 但 skill 字段非 dict = 空内容呈现，不算失败。"""
        async def weird_get(method: str, args: dict[str, Any]) -> dict[str, Any]:
            if method == "list":
                return {"items": [_skill_item("one", "One Skill", "d")]}
            return {"found": True, "skill": "not-a-dict"}
        sys.modules["resource_search_tool_impl"].set_skill_service_caller(weird_get)
        out = _run(rs, resource_type="skill", mode="detailed")
        assert out["skill_h"] == ["skill_name", "skill_description", "skill_content"]
        assert out["skill_d"][0][2] == ""


class TestToolSurfaceReconciliation:
    """T4 新增：manifest 扫描 ∩ 内核活注册表（禁用插件工具不再出现）。"""

    def test_scan_without_live_entry_not_listed(self, rs: Any, repo: Path) -> None:
        """扫描有+活列表无（禁用/卸载插件的工具）→ 不出现。"""
        _install_tool_surface([_live_item("download_file")])
        out = _run(rs, resource_type="tool")
        assert _names(out, "tool") == ["download_file"]

    def test_both_sides_present_listed(self, rs: Any, repo: Path) -> None:
        """两边都有 → 出现（对账不吞活工具）。"""
        _install_tool_surface([_live_item(n) for n in MINI_REPO_TOOLS])
        out = _run(rs, resource_type="tool")
        assert set(_names(out, "tool")) == set(MINI_REPO_TOOLS)
        assert out["tool_c"] == len(MINI_REPO_TOOLS)

    def test_live_only_tool_not_listed(self, rs: Any, repo: Path) -> None:
        """活列表有+扫描无（行源=扫描）→ 不出现。"""
        _install_tool_surface([
            _live_item("download_file"),
            _live_item("ghost_tool", plugin_id="elsewhere"),
        ])
        assert "ghost_tool" not in _names(_run(rs, resource_type="tool"), "tool")

    def test_row_description_from_manifest_not_live(self, rs: Any, repo: Path) -> None:
        """行源裁定锁：活条目描述与 manifest 不一致时，行取 manifest 扫描值。"""
        _install_tool_surface([_live_item("download_file", description="LIVE DESC")])
        out = _run(rs, resource_type="tool", query="download")
        assert out["tool_d"][0] == ["download_file", "download remote files"]

    def test_disabled_plugin_tools_absent(self, rs: Any, repo: Path) -> None:
        """禁用模拟 = tool_b 全员从活列表剔除 → bash_execute 消失，其余照常。"""
        _install_tool_surface([_live_item("download_file"), _live_item("upload_file")])
        out = _run(rs, resource_type="tool", query="*")
        assert _names(out, "tool") == ["download_file", "upload_file"]
        assert out["tool_c"] == 2

    def test_empty_live_registry_empty_result(self, rs: Any, repo: Path) -> None:
        """活注册表空（registry 未装配装配态）= 空结果键缺席，不是失败。"""
        _install_tool_surface([])
        assert _run(rs, resource_type="tool") == {}

    def test_response_without_items_is_empty_result(self, rs: Any, repo: Path) -> None:
        """响应无 items 键（降级形态）= 空活集 → 空结果键缺席，不算失败。"""
        async def no_items() -> dict[str, Any]:
            return {"total": 0}

        sys.modules["resource_search_tool_impl"].set_tool_surface_caller(no_items)
        assert _run(rs, resource_type="tool") == {}

    def test_surface_fetched_once_per_execute(self, rs: Any, repo: Path) -> None:
        calls = _install_tool_surface([_live_item(n) for n in MINI_REPO_TOOLS])
        _run(rs, resource_type="tool")
        assert len(calls) == 1


class TestToolSurfaceFailure:
    """活注册表对账通道不可用/返回无效 = 显式失败信封（T4 fail-closed 禁令）。"""

    @staticmethod
    def _execute(rs: Any, **inputs: Any) -> Any:
        return asyncio.run(rs.execute({"resource_type": "tool", **inputs}))

    def test_caller_not_injected(self, rs: Any) -> None:
        # 扫描面（真实仓清单）可用也必失败：对账是本刀存在意义，不回落纯扫描
        result = self._execute(rs)
        assert result.success is False
        assert result.error
        assert "未注入" in result.error
        assert result.error_code == "tool_surface_unavailable"

    def test_caller_raises(self, rs: Any) -> None:
        async def boom() -> dict[str, Any]:
            raise RuntimeError("kernel unreachable")

        sys.modules["resource_search_tool_impl"].set_tool_surface_caller(boom)
        result = self._execute(rs)
        assert result.success is False
        assert "kernel unreachable" in (result.error or "")
        assert result.error_code == "tool_surface_unavailable"

    def test_non_dict_response_is_failure(self, rs: Any) -> None:
        async def malformed() -> Any:
            return ["not", "a", "dict"]

        sys.modules["resource_search_tool_impl"].set_tool_surface_caller(malformed)
        result = self._execute(rs)
        assert result.success is False
        assert "返回无效" in (result.error or "")

    def test_items_not_list_is_failure(self, rs: Any) -> None:
        async def bad_items() -> dict[str, Any]:
            return {"items": {"oops": 1}, "total": 1}

        sys.modules["resource_search_tool_impl"].set_tool_surface_caller(bad_items)
        result = self._execute(rs)
        assert result.success is False
        assert "非列表" in (result.error or "")

    def test_all_type_fails_fast_no_partial(self, rs: Any, agents: list) -> None:
        """resource_type=all 且对账通道失败 → 整体失败信封，不出部分成功载荷。"""
        async def failing() -> dict[str, Any]:
            raise RuntimeError("kernel down")

        sys.modules["resource_search_tool_impl"].set_tool_surface_caller(failing)
        result = asyncio.run(rs.execute({"resource_type": "all"}))
        assert result.success is False
        assert result.error
        assert "kernel down" in result.error
        assert not result.output or not any(
            k.startswith(("agent_", "tool_", "skill_")) for k in result.output
        )


class TestAgentServiceFailure:
    """agent 注册表服务不可用/调用失败 = 显式失败信封（T2 fail-closed 禁令）。"""

    @staticmethod
    def _execute(rs: Any, **inputs: Any) -> Any:
        return asyncio.run(rs.execute({"resource_type": "agent", **inputs}))

    def test_caller_not_injected(self, rs: Any) -> None:
        # 新装载实例模块级 caller 为 None（on_load 未跑）——显式失败而非空结果
        result = self._execute(rs)
        assert result.success is False
        assert result.error
        assert "未注入" in result.error

    def test_caller_raises(self, rs: Any) -> None:
        async def boom(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("capability not injected: tool-executor")
        sys.modules["resource_search_tool_impl"].set_agent_service_caller(boom)
        result = self._execute(rs)
        assert result.success is False
        assert result.error
        assert "capability not injected" in result.error

    def test_service_failure_envelope_no_fake_success(self, rs: Any) -> None:
        async def failing(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"success": False, "error": "工具 agent.list 未注册"}
        sys.modules["resource_search_tool_impl"].set_agent_service_caller(failing)
        result = self._execute(rs)
        assert result.success is False
        assert result.error
        assert "agent.list 未注册" in result.error
        # 失败信封不携带 agent_* 键假成功
        assert not result.output or not any(
            k.startswith("agent_") for k in result.output
        )

    def test_all_type_fails_fast_no_partial(self, rs: Any) -> None:
        """resource_type=all 且 agent 服务失败 → 整体失败信封，不出部分成功载荷。"""
        async def failing(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"success": False, "error": "agent_manager disabled"}
        sys.modules["resource_search_tool_impl"].set_agent_service_caller(failing)
        result = asyncio.run(rs.execute({"resource_type": "all"}))
        assert result.success is False
        assert result.error
        assert "agent_manager disabled" in result.error
        assert not result.output or not any(
            k.startswith(("tool_", "skill_")) for k in result.output
        )


class TestAgentManagerIntegration:
    """进程内集成（真实依赖 ≥1 条）：真实 agent_manager 聚合函数 + 仓内真实语料。

    transport 信封由本测试按 tool-executor 归一形态包一层（Rust 侧
    ToolExecutionResult {success, data}），聚合数据本身来自
    plugins/shared/agent_manager 的真实 aggregate_agents。
    """

    @staticmethod
    def _load_agent_manager() -> Any:
        name = "agent_manager_real_aggregate"
        if name in sys.modules:
            return sys.modules[name]
        src = _PLUGIN_DIR.parent / "agent_manager" / "server.py"
        spec = importlib.util.spec_from_file_location(name, src)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def test_real_aggregate_feeds_mapping(self, rs: Any) -> None:
        am = self._load_agent_manager()
        real = am.aggregate_agents()
        assert real["total"] > 0, "仓内 config/agents 语料非空前提"

        async def real_service(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            assert tool_name == "agent.list"
            return {"success": True, "data": am.aggregate_agents()}

        sys.modules["resource_search_tool_impl"].set_agent_service_caller(real_service)
        out = _run(rs, resource_type="agent")
        assert out["agent_c"] == min(real["total"], LIMIT_CAP)  # limit 收敛性质
        assert out["agent_c"] == len(out["agent_d"])
        assert out["agent_h"] == [
            "config_id", "agent_name", "agent_description", "level", "source",
        ]
        for row in out["agent_d"]:
            assert row[0]  # config_id 非空（聚合条目身份字段）
            assert row[3] in ("", "L1", "L2", "L3")  # level 如实映射
            assert row[4] == "system"  # 服务面无 token → 模式面降级，全系统空间
        # 真实语料可搜：按第一条目名/描述任一非空词反查能命中该条目
        first = out["agent_d"][0]
        needle = (first[1] or first[2] or first[0]).split()[0].lower()
        hit = _run(rs, resource_type="agent", query=needle)
        assert first[0] in {row[0] for row in hit.get("agent_d", [])}


class TestSkillServiceFailure:
    """skill 注册表服务不可用/调用失败 = 显式失败信封（T3 fail-closed 禁令）。"""

    @staticmethod
    def _execute(rs: Any, **inputs: Any) -> Any:
        return asyncio.run(rs.execute({"resource_type": "skill", **inputs}))

    def test_caller_not_injected(self, rs: Any) -> None:
        result = self._execute(rs)
        assert result.success is False
        assert result.error
        assert "未注入" in result.error

    def test_caller_raises(self, rs: Any) -> None:
        async def boom(method: str, args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("capability not injected: skill")
        sys.modules["resource_search_tool_impl"].set_skill_service_caller(boom)
        result = self._execute(rs)
        assert result.success is False
        assert result.error
        assert "capability not injected" in result.error

    def test_service_error_dict_no_fake_success(self, rs: Any) -> None:
        async def failing(method: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"error": "skill.list 未注册"}
        sys.modules["resource_search_tool_impl"].set_skill_service_caller(failing)
        result = self._execute(rs)
        assert result.success is False
        assert result.error
        assert "skill.list 未注册" in result.error
        assert not result.output or not any(
            k.startswith("skill_") for k in result.output
        )

    def test_non_dict_result_is_failure(self, rs: Any) -> None:
        async def malformed(method: str, args: dict[str, Any]) -> Any:
            return ["not", "a", "dict"]
        sys.modules["resource_search_tool_impl"].set_skill_service_caller(malformed)
        result = self._execute(rs)
        assert result.success is False
        assert "返回无效" in (result.error or "")

    def test_items_not_list_is_failure(self, rs: Any) -> None:
        async def bad_items(method: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"items": {"oops": 1}}
        sys.modules["resource_search_tool_impl"].set_skill_service_caller(bad_items)
        result = self._execute(rs)
        assert result.success is False
        assert "非列表" in (result.error or "")

    def test_all_type_fails_fast_no_partial(self, rs: Any, repo: Path, agents: list) -> None:
        """resource_type=all 且 skill 服务失败 → 整体失败信封，不出部分成功载荷。"""
        async def failing(method: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"error": "skill_manager disabled"}
        sys.modules["resource_search_tool_impl"].set_skill_service_caller(failing)
        result = asyncio.run(rs.execute({"resource_type": "all"}))
        assert result.success is False
        assert "skill_manager disabled" in result.error
        assert not result.output or not any(
            k.startswith(("agent_", "tool_")) for k in result.output
        )


class TestSkillManagerIntegration:
    """进程内集成（真实依赖 ≥1 条）：真实 skill_manager 枚举函数 + 仓内真实 skills/。

    装载 plugins/shared/skill_manager/skill_registry 真模块，list_skills() 吃
    仓内 skills/ 语料，经调用缝桩喂给消费侧映射层——transport 裸 dict 契约由
    本桩承载（provides 直连通路的 Rust 侧转发对结果 Value 透传）。
    """

    def test_real_registry_feeds_mapping(self, rs: Any) -> None:
        shared_root = _PLUGIN_DIR.parents[1]
        if str(shared_root) not in sys.path:
            sys.path.insert(0, str(shared_root))
        from skill_manager import skill_registry

        real = skill_registry.list_skills()
        assert len(real) > 0, "仓内 skills/ 语料非空前提"

        async def real_service(method: str, args: dict[str, Any]) -> dict[str, Any]:
            if method == "list":
                return {"items": skill_registry.list_skills()}
            return skill_registry.get_skill(str(args.get("skill_id")))

        sys.modules["resource_search_tool_impl"].set_skill_service_caller(real_service)
        out = _run(rs, resource_type="skill")
        assert out["skill_c"] == min(len(real), LIMIT_CAP)  # limit 收敛性质
        assert out["skill_c"] == len(out["skill_d"])
        assert out["skill_h"] == ["skill_name", "skill_description"]
        for row in out["skill_d"]:
            assert row[0]  # name 非空（解析兜底链保证）
        # 真实语料可搜：按第一条目名反查能命中该条目
        first = out["skill_d"][0]
        needle = first[0].split()[0].lower()
        hit = _run(rs, resource_type="skill", query=needle)
        assert first[0] in {row[0] for row in hit.get("skill_d", [])}

    def test_real_registry_detailed_content_via_get(self, rs: Any) -> None:
        """真实语料 detailed：skill.get 取正文，内容列非空（front matter 剥离后）。"""
        shared_root = _PLUGIN_DIR.parents[1]
        if str(shared_root) not in sys.path:
            sys.path.insert(0, str(shared_root))
        from skill_manager import skill_registry

        async def real_service(method: str, args: dict[str, Any]) -> dict[str, Any]:
            if method == "list":
                return {"items": skill_registry.list_skills()}
            return skill_registry.get_skill(str(args.get("skill_id")))

        sys.modules["resource_search_tool_impl"].set_skill_service_caller(real_service)
        out = _run(rs, resource_type="skill", mode="detailed", limit=3)
        assert out["skill_h"] == ["skill_name", "skill_description", "skill_content"]
        assert any(row[2].strip() for row in out["skill_d"])


class TestConcurrentFaces:
    """并发化改造的行为契约（2026-11-19 搜索性能修复）。

    串行时代的可见输出（id 序、limit 截断、失败信封优先序）必须逐位保持，
    同时断并发性本身：①三资源面的首个外部调用在任一面完成前全部发出
    （串行退化 = 护栏轮询超时 → 面失败信封，本测试红）；②skill 正文兜底与
    agent detailed 补全的成批取数并发窗 > 1（串行退化恒为 1）。
    """

    def test_faces_issue_concurrently(self, rs: Any, repo: Path) -> None:
        """三面护栏桩：任一面完成前，三面的首个外部调用必须都已发出。"""
        impl = sys.modules["resource_search_tool_impl"]
        issued = {"agent": False, "tool": False, "skill": False}

        async def gate(key: str) -> None:
            issued[key] = True
            for _ in range(300):  # ~0.3s 护栏：串行退化下永远凑不齐 → 显式失败
                if all(issued.values()):
                    return
                await asyncio.sleep(0.001)
            raise AssertionError(f"资源面未并发发起: issued={issued}")

        async def agent_stub(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            await gate("agent")
            return {
                "success": True,
                "data": {"items": [_agg("alpha_agent", "Alpha Agent", "helper")], "total": 1},
            }

        async def tool_stub() -> dict[str, Any]:
            await gate("tool")
            return {"items": [_live_item("download_file")], "total": 1}

        async def skill_stub(method: str, args: dict[str, Any]) -> dict[str, Any]:
            await gate("skill")
            if method == "list":
                return {"items": [_skill_item("one", "One Skill", "the first skill")]}
            return {"found": False}

        impl.set_agent_service_caller(agent_stub)
        impl.set_tool_surface_caller(tool_stub)
        impl.set_skill_service_caller(skill_stub)
        out = _run(rs, resource_type="all")
        assert {"agent_c", "tool_c", "skill_c"} <= set(out)

    def test_skill_body_fallback_concurrent_window(self, rs: Any) -> None:
        """正文兜底取数并发窗 > 1（串行实现恒为 1）；命中集与串行语义一致。"""
        impl = sys.modules["resource_search_tool_impl"]
        state = {"inflight": 0, "max": 0}
        items = [_skill_item(f"s{i}", f"Skill {i}", f"generic description {i}") for i in range(6)]
        contents = {f"s{i}": f"body text with needle {i}" for i in range(6)}

        async def stub(method: str, args: dict[str, Any]) -> dict[str, Any]:
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
            await asyncio.sleep(0.01)
            state["inflight"] -= 1
            if method == "list":
                return {"items": items}
            sid = str(args.get("skill_id"))
            if sid in contents:
                return {"found": True, "skill": {**items[int(sid[1:])], "content": contents[sid]}}
            return {"found": False}

        impl.set_skill_service_caller(stub)
        out = _run(rs, resource_type="skill", query="needle")
        assert out["skill_c"] == 6
        assert [row[0] for row in out["skill_d"]] == [f"Skill {i}" for i in range(6)]
        assert state["max"] > 1, f"兜底取数未并发（max_inflight={state['max']}）"

    def test_skill_body_fallback_order_and_limit(self, rs: Any) -> None:
        """id 序组装 + limit 截断与串行时代逐位一致（两组区分输入）。

        语料：c1 正文命中、c2 无命中、c3 正文命中、c4 元数据命中、c5 正文
        命中（id 序 c1<c2<c3<c4<c5；元数据命中不触发兜底取数）。
        """
        items = [
            _skill_item("c1", "C One", "plain"),
            _skill_item("c2", "C Two", "plain"),
            _skill_item("c3", "C Three", "plain"),
            _skill_item("c4", "C Four", "needle in description"),
            _skill_item("c5", "C Five", "plain"),
        ]
        contents = {"c1": "needle in body", "c3": "another needle", "c5": "third needle"}
        calls = _install_skill_service(items, contents=contents)
        out = _run(rs, resource_type="skill", query="needle")
        assert out["skill_c"] == 4
        assert [row[0] for row in out["skill_d"]] == ["C One", "C Three", "C Four", "C Five"]
        out2 = _run(rs, resource_type="skill", query="needle", limit=2)
        assert [row[0] for row in out2["skill_d"]] == ["C One", "C Three"]
        # 通配查询零兜底取数（契约保持：无 get 调用）
        calls.clear()
        _run(rs, resource_type="skill", query="*")
        assert not [c for c in calls if c[0] == "get"]

    def test_agent_detailed_concurrent_window(self, rs: Any) -> None:
        """detailed 模式 agent.get 补全并发窗 > 1；metrics 列逐行呈现。"""
        impl = sys.modules["resource_search_tool_impl"]
        state = {"inflight": 0, "max": 0}
        items = [
            _agg(f"agent_{i}", f"Agent {i}", f"desc {i}", level="L1") for i in range(5)
        ]
        configs = {
            f"agent_{i}": {"recommended_metrics": [{"metric_id": f"m{i}"}]}
            for i in range(5)
        }

        async def stub(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
            await asyncio.sleep(0.01)
            state["inflight"] -= 1
            if tool_name == "agent.list":
                return {"success": True, "data": {"items": items, "total": len(items)}}
            aid = str(args.get("agent_id"))
            if aid in configs:
                return {"success": True, "data": {"found": True, "config": configs[aid]}}
            return {"success": True, "data": {"found": False, "config": None}}

        impl.set_agent_service_caller(stub)
        out = _run(rs, resource_type="agent", mode="detailed")
        assert out["agent_c"] == 5
        assert [f"推荐评估: m{i}" in row[-1] for i, row in enumerate(out["agent_d"])] == [True] * 5
        assert state["max"] > 1, f"agent.get 补全未并发（max_inflight={state['max']}）"

    def test_face_failure_priority_agent_first(self, rs: Any, repo: Path, skills: list) -> None:
        """agent/tool/skill 三面同败 → 失败信封取 agent 面（原串行短路顺序）。"""
        impl = sys.modules["resource_search_tool_impl"]

        async def agent_down(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"success": False, "error": "agent_manager down"}

        async def skill_down(method: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"error": "skill_manager down"}

        impl.set_agent_service_caller(agent_down)
        impl.set_tool_surface_caller(_failing_tool_surface)
        impl.set_skill_service_caller(skill_down)
        result = asyncio.run(rs.execute({"resource_type": "all"}))
        assert result.success is False
        assert result.error_code == "agent_registry_unavailable"
        assert "agent_manager down" in (result.error or "")

    def test_face_failure_priority_tool_over_skill(self, rs: Any, repo: Path, skills: list) -> None:
        """agent 面成功、tool/skill 同败 → 失败信封取 tool 面。"""
        impl = sys.modules["resource_search_tool_impl"]

        async def skill_down(method: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"error": "skill_manager down"}

        impl.set_agent_service_caller(_empty_agent_service)
        impl.set_tool_surface_caller(_failing_tool_surface)
        impl.set_skill_service_caller(skill_down)
        result = asyncio.run(rs.execute({"resource_type": "all"}))
        assert result.success is False
        assert result.error_code == "tool_surface_unavailable"


async def _failing_tool_surface() -> dict[str, Any]:
    raise RuntimeError("tools endpoint down")


async def _empty_agent_service(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "agent.list":
        return {"success": True, "data": {"items": [], "total": 0}}
    return {"success": True, "data": {"found": False, "config": None}}
