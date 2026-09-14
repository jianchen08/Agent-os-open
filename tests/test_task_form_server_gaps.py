# @feature: FP-0.2.二 任务表单插件服务 | @ci: python-coverage
"""task_form 服务缺口分支补测（coverage.xml 缺口行靶单）。

经公开工具入口 http.handle + 模块级端点行为驱动（不直接断言私有实现）：

1. ``_project_root`` 解析：AGENTOS_PROJECT_ROOT 命中即用（含指向不存在目录时
   忽略）；env 无效 → 从 CWD 向上找含 config/ 的祖先；上溯耗尽/触顶无 config
   → 回退当前 CWD（不抛，读面不崩）。
2. ``_load_form_fields``：非 dict 顶层（列表/标量）返回空；顶层无 form 段时
   回落顶层 fields；form 段存在但非 dict 时同回落；fields 条目非 dict 剔除。
3. YAML 损坏 / 文件不可读 → warning 留痕 + 空字段表（读面不崩）。
4. ``_agent_options``：agents 目录缺失 → 空列表；损坏 YAML 文件跳过不炸；
   解析结果非 dict 跳过；config_id 缺失回落文件名 stem、name 缺失回落
   display_name、两者皆缺回落 value。
5. 端点：未知 path → 404 信封（path 回显）；方法不匹配（POST 到 GET 端点）
   → 404（路由判据含 method）。

外部依赖仅临时 config 目录与 CWD（monkeypatch.chdir），不接真实内核/服务。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "shared" / "system" / "task_form"
_MODULE_NAME = "task_form_server_gaps_under_test"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(_PLUGIN_DIR / "server.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """干净环境：清掉 AGENTOS_PROJECT_ROOT / TASKS_STORAGE_DIR 让用例自持定位。"""
    monkeypatch.delenv("AGENTOS_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "tasks_data"))
    return _load_module()


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    return json.loads(base64.b64decode(result["data"]["body"]))


def _make_project(tmp_path: Path, form_yaml: str | None, agents: dict[str, str] | None = None) -> Path:
    """搭最小项目根：config/task_form.yaml（可选）+ config/agents/**.yaml。"""
    root = tmp_path / "proj"
    config = root / "config"
    (config / "agents").mkdir(parents=True, exist_ok=True)
    if form_yaml is not None:
        (config / "task_form.yaml").write_text(form_yaml, encoding="utf-8")
    for rel, content in (agents or {}).items():
        path = config / "agents" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


# ═══════════════════ 项目根解析 ═══════════════════


class TestProjectRootResolution:
    def test_env_project_root_wins(self, server: Any, tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
        root = _make_project(tmp_path, "form: {fields: []}\n")
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        assert Path(server._project_root()) == root

    def test_invalid_env_ignored_and_ancestor_probed(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """env 指向不存在目录 → 忽略；从 CWD 向上找到含 config/ 的祖先。"""
        root = _make_project(tmp_path, "form: {fields: []}\n")
        deep = root / "a" / "b"
        deep.mkdir(parents=True)
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path / "ghost"))
        monkeypatch.chdir(deep)

        assert Path(server._project_root()) == root

    def test_no_config_ancestor_falls_back_to_cwd(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """上溯链无 config/ → 回退当前 CWD（读面不崩，返回可预期的基目录）。"""
        orphan = tmp_path / "orphan"
        orphan.mkdir()
        monkeypatch.chdir(orphan)

        assert Path(server._project_root()) == orphan

    def test_ancestor_walk_stops_at_filesystem_root(
        self, server: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """上溯到文件系统根（父目录 == 自身且无 config/）→ 立即停止并回落该根。"""
        root = Path("C:/") if os.name == "nt" else Path("/")
        monkeypatch.setattr(os, "getcwd", lambda: str(root))

        assert Path(server._project_root()) == root


# ═══════════════════ 表单字段声明 ═══════════════════


class TestLoadFormFields:
    def test_form_block_fields(self, server: Any, tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
        root = _make_project(tmp_path, """
form:
  fields:
    - { name: title, type: input }
    - { name: target_id, type: select }
""")
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        fields = server._load_form_fields()

        assert [f["name"] for f in fields] == ["title", "target_id"]

    def test_non_dict_form_block_falls_back_to_top_level_fields(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """form 段存在但非 dict → 回落顶层 fields（历史声明形态）。"""
        root = _make_project(
            tmp_path, "form: not-a-dict\nfields:\n  - { name: b, type: input }\n"
        )
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        assert [f["name"] for f in server._load_form_fields()] == ["b"]

    def test_absent_form_key_yields_empty_even_with_top_level_fields(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """现状契约：无 form 键时走 form 缺省空 dict 分支，顶层 fields 不被消费。"""
        root = _make_project(tmp_path, "fields:\n  - { name: a, type: input }\n")
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        assert server._load_form_fields() == []

    @pytest.mark.parametrize(
        "yaml_text",
        [
            "- just\n- a\n- list\n",          # 顶层为列表
            "plain-scalar\n",                  # 顶层为标量
            "null\n",                          # 空文档
            "",                                # 空文件
        ],
    )
    def test_non_dict_or_empty_document_yields_empty(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yaml_text: str,
    ) -> None:
        """非 dict 顶层 → 空字段表（不抛、不猜结构）。"""
        root = _make_project(tmp_path, yaml_text)
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        assert server._load_form_fields() == []

    def test_non_dict_field_entries_filtered(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """fields 数组内非 dict 条目剔除（前端 RJSF 只吃对象声明）。"""
        root = _make_project(tmp_path, """
form:
  fields:
    - { name: keep, type: input }
    - just-a-string
    - 42
    - null
""")
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        assert [f["name"] for f in server._load_form_fields()] == ["keep"]

    def test_missing_file_returns_empty_with_warning(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """声明文件缺失 → warning 留痕 + 空字段表（插件读面不崩）。"""
        root = _make_project(tmp_path, None)
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        with caplog.at_level("WARNING"):
            assert server._load_form_fields() == []

        assert any("表单声明读取失败" in r.message for r in caplog.records)

    def test_broken_yaml_returns_empty_with_warning(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """YAML 语法错误 → warning 留痕 + 空字段表（损坏不伪装成空声明）。"""
        root = _make_project(tmp_path, "form: [unclosed\n")
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        with caplog.at_level("WARNING"):
            assert server._load_form_fields() == []

        assert any("表单声明读取失败" in r.message for r in caplog.records)


# ═══════════════════ 执行 Agent 选项 ═══════════════════


class TestAgentOptions:
    def test_absent_agents_dir_returns_empty(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "proj"
        (root / "config").mkdir(parents=True)
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        assert server._agent_options() == []

    def test_config_id_and_name_extracted_nested(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """含嵌套子目录：value=config_id、label=name，按 label 排序。"""
        root = _make_project(tmp_path, None, {
            "main/agentos.yaml": "config_id: agentos\nname: 灵汐\n",
            "executor/general_agent.yaml": "config_id: general_agent\nname: 通用任务执行者\n",
        })
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        options = server._agent_options()

        assert options == [
            {"value": "agentos", "label": "灵汐"},
            {"value": "general_agent", "label": "通用任务执行者"},
        ]
        assert [o["label"] for o in options] == sorted(o["label"] for o in options)

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ("name: 无名\n", {"value": "no_id", "label": "无名"}),          # config_id 缺 → stem
            ("config_id: cid\ndisplay_name: 显示名\n", {"value": "cid", "label": "显示名"}),
            ("config_id: cid\n", {"value": "cid", "label": "cid"}),          # label 回退 value
            ("{}\n", {"value": "empty_cfg", "label": "empty_cfg"}),          # 两者皆缺
        ],
    )
    def test_value_label_fallbacks(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        content: str, expected: dict[str, str],
    ) -> None:
        stem = expected["value"]
        root = _make_project(tmp_path, None, {f"main/{stem}.yaml": content})
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        assert server._agent_options() == [expected]

    def test_broken_and_non_dict_yaml_skipped(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """损坏 YAML 与非 dict 文档跳过，其余条目照常返回（单文件坏不阻断）。"""
        root = _make_project(tmp_path, None, {
            "main/good.yaml": "config_id: good\nname: 好\n",
            "main/broken.yaml": "a: [unclosed\n",
            "main/scalar.yaml": "just-a-string\n",
            "main/list.yaml": "- a\n- b\n",
        })
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        options = server._agent_options()

        assert options == [{"value": "good", "label": "好"}]


# ═══════════════════ HTTP 端点 ═══════════════════


class TestHttpRouting:
    async def test_form_endpoint_uses_declaration_file(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = _make_project(tmp_path, "form:\n  fields:\n    - { name: t, type: input }\n")
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        resp = await server.http_handle(path="/ext/task_form/form", method="GET", query={})

        body = _payload(resp)
        assert resp["data"]["status"] == 200
        assert body["fields"] == [{"name": "t", "type": "input"}]
        assert (body["id"], body["title"]) == ("task_create", "新建任务")

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/ext/task_form/form", "POST"),            # 端点仅接受 GET
            ("/ext/task_form/options/agents", "POST"),
            ("/ext/task_form/options/projects", "POST"),
            ("/ext/task_form/unknown", "GET"),
            ("/ext/task_form/options", "GET"),          # 缺子资源段
            ("/ext/task_form/form/extra", "GET"),       # 段数过多
            ("/", "GET"),
        ],
    )
    async def test_unrouted_returns_404_with_path(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        path: str, method: str,
    ) -> None:
        """未匹配（含方法不符）→ 404 信封且回显 path（前端可归因）。"""
        root = _make_project(tmp_path, "form: {fields: []}\n")
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))

        resp = await server.http_handle(path=path, method=method, query={})

        assert resp["success"] is True
        assert resp["data"]["status"] == 404
        assert _payload(resp) == {"error": "not found", "path": path}

    async def test_projects_endpoint_reads_shared_registry(
        self, server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """项目选项 = 共享登记行 {value:id,label:title}；无登记 → 空列表无 warning。"""
        root = _make_project(tmp_path, "form: {fields: []}\n")
        monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(root))
        monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "tasks_data"))
        from project_registry import ProjectModel, ProjectRegistry

        ProjectRegistry(tmp_path / "tasks_data").save(
            ProjectModel(id="p0000000001", title="项目甲", path="D:/a")
        )

        resp = await server.http_handle(
            path="/ext/task_form/options/projects", method="GET", query={},
        )

        assert _payload(resp)["data"] == [{"value": "p0000000001", "label": "项目甲"}]
