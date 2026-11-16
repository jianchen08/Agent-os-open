# @feature: FP-0.2.dev_toolkit | @ci: python-coverage
"""插件开发技能脚本单测：骨架生成 / 版本纪律迁移 / 装载诊断 / 无 LLM 直调。

行为契约（断输入→输出，防拟合：字面值配性质断言、≥2 组有区分度输入）：
- scaffold_functional：theme/page 生成多文件集且 plugin.json 合法（tools==[]，
  http.handle 是运行面注册——dsh_adapter 判例）；page.html 过 CSP 禁串；
  拒绝面（非法 id/kind 越界/目标已存在）全部 ScaffoldError 不落半成品；
- promote_plugin：BuiltinFirst 版本纪律（严格大于才迁，持平/倒退/非法版本一律拒，
  预发布段 rc.x < 正式版）；覆盖迁移留 .bak 备份；dry-run 零落盘；
- plugin_doctor：声明完整性按 resource-tool-create 六字段契约判；
  tool_ids 未登记判 fail 并给修复指引；doctor verdict 汇总硬检查；
- debug_tool：http.handle 型插件经 server.py 模块级 handle 直调；
  入参不匹配实现签名显式报错；
- spawn_test_kernel.spawn：kernel_bin 直指时绕过仓内产物解析（装机自举车道）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "plugin-dev" / "scripts"
TOOLKIT_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "skill-dev-toolkit" / "scripts"


def _load(name: str, scripts_dir: Path = SCRIPTS_DIR):
    spec = importlib.util.spec_from_file_location(name, scripts_dir / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


scaffold_functional = _load("scaffold_functional")
promote_plugin = _load("promote_plugin")
plugin_doctor = _load("plugin_doctor")
debug_tool = _load("debug_tool")
spawn_test_kernel = _load("spawn_test_kernel", TOOLKIT_SCRIPTS)
verify_plugin = _load("verify_plugin")

pytestmark = pytest.mark.unit


# ── scaffold_functional ──────────────────────────────────────────────────

def _make(tmp_path: Path, kind: str, plugin_id: str) -> Path:
    created = scaffold_functional.scaffold_functional(
        tmp_path, plugin_id, kind, "演示功能性插件"
    )
    assert len(created) == 4
    return tmp_path / plugin_id


def test_theme_scaffold_end_to_end(tmp_path: Path) -> None:
    target = _make(tmp_path, "theme", "demo_theme")
    manifest = json.loads((target / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["capabilities"]["tools"] == []  # http.handle 是运行面注册
    theme = manifest["contributes"]["themes"][0]
    assert theme["id"] == "demo_theme-theme"
    assert theme["base"] in ("light", "dark")
    assert "--ds-bg-canvas" in theme["variables"]
    endpoint = manifest["http_endpoints"][0]
    assert endpoint["path"] == "/ext/demo_theme/styles/theme.css"
    assert endpoint["handler_capability"] == "http.handle"


def test_page_scaffold_passes_csp_forbidden_scan(tmp_path: Path) -> None:
    target = _make(tmp_path, "page", "demo_page")
    manifest = json.loads((target / "plugin.json").read_text(encoding="utf-8"))
    page = manifest["contributes"]["pages"][0]
    assert page["widget"] == "webview"
    assert page["props"]["pluginId"] == "demo_page"
    html = (target / "page.html").read_text(encoding="utf-8")
    assert len(re.findall(r"<script", html)) == 1
    forbidden = re.compile(r"\bsrc=|\bhref=|url\(|fetch\(|websocket|XMLHttpRequest|import\(")
    assert not forbidden.search(html)


def test_scaffold_reject_invalid_id_and_kind(tmp_path: Path) -> None:
    with pytest.raises(scaffold_functional.ScaffoldError):
        scaffold_functional.scaffold_functional(tmp_path, "Bad-Id", "theme", "x")
    with pytest.raises(scaffold_functional.ScaffoldError):
        scaffold_functional.scaffold_functional(tmp_path, "ok_id", "settings_panel", "x")


def test_scaffold_reject_existing_target(tmp_path: Path) -> None:
    scaffold_functional.scaffold_functional(tmp_path, "dup_theme", "theme", "x")
    with pytest.raises(scaffold_functional.ScaffoldError):
        scaffold_functional.scaffold_functional(tmp_path, "dup_theme", "theme", "x")


# ── promote_plugin：BuiltinFirst 版本纪律 ────────────────────────────────

@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("1.1.0", "1.0.0", True),      # 正常升级
        ("2.0.0", "1.9.9", True),      # 跨主版本
        ("1.0.0", "1.0.0", False),     # 持平=同步无效
        ("0.9.0", "1.0.0", False),     # 倒退
        ("1.0.0-rc.1", "1.0.0", False),  # 预发布 < 正式版
        ("1.0.0-rc.2", "1.0.0-rc.1", True),  # 预发布段内部可比
        ("not-a-version", "1.0.0", False),   # 非法版本 fail-closed
    ],
)
def test_is_strictly_greater(candidate: str, current: str, expected: bool) -> None:
    assert promote_plugin.is_strictly_greater(candidate, current) is expected


def test_promote_first_install_and_same_version_rejected(tmp_path: Path) -> None:
    staged = _make(tmp_path, "theme", "demo_theme")
    report = promote_plugin.promote(staged, tmp_path / "installed")
    assert report["promoted"] is True
    assert report["backup"] is None
    assert (tmp_path / "installed" / "demo_theme" / "plugin.json").is_file()
    with pytest.raises(promote_plugin.PromoteError, match="版本纪律不满足"):
        promote_plugin.promote(staged, tmp_path / "installed")  # 同版重推拒绝


def test_promote_overwrite_keeps_backup(tmp_path: Path) -> None:
    staged = _make(tmp_path, "theme", "demo_theme")
    promote_plugin.promote(staged, tmp_path / "installed")
    manifest_path = staged / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "0.2.0"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    report = promote_plugin.promote(staged, tmp_path / "installed")
    assert report["promoted"] is True
    assert report["backup"]
    assert report["backup"].startswith("demo_theme.bak-")


def test_promote_dry_run_touches_nothing(tmp_path: Path) -> None:
    staged = _make(tmp_path, "theme", "demo_theme")
    report = promote_plugin.promote(staged, tmp_path / "installed", dry_run=True)
    assert report["promoted"] is False
    assert not (tmp_path / "installed").exists()


# ── plugin_doctor ────────────────────────────────────────────────────────

def _write_plugin(tmp_path: Path, tools: list[dict]) -> Path:
    target = tmp_path / "doc_plugin"
    target.mkdir(parents=True)
    manifest = {
        "id": "doc_plugin", "name": "DocPlugin", "version": "0.1.0",
        "entry": "python server.py",
        "capabilities": {"tools": tools}, "permissions": {},
    }
    (target / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return target


_FULL_TOOL = {
    "name": "doc_plugin", "description": "d",
    "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
    "render": {"card": "form"},
}


def test_doctor_manifest_full_and_missing_schema(tmp_path: Path) -> None:
    good = _write_plugin(tmp_path / "a", [_FULL_TOOL])
    check, manifest = plugin_doctor.check_manifest(good)
    assert check["pass"]
    assert manifest["id"] == "doc_plugin"
    bad = _write_plugin(tmp_path / "b", [{**_FULL_TOOL, "output_schema": None}])
    check, _ = plugin_doctor.check_manifest(bad)
    assert not check["pass"]
    assert "output_schema" in check["detail"]


def test_doctor_tool_ids_registered_vs_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    agents = repo / "config" / "agents" / "main"
    agents.mkdir(parents=True)
    (agents / "agentos.yaml").write_text(
        "tool_ids: [doc_plugin, other_tool]\n", encoding="utf-8"
    )
    plugin_dir = _write_plugin(tmp_path, [_FULL_TOOL])
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    registered = plugin_doctor.check_tool_ids(plugin_dir, repo, manifest)
    assert registered["pass"]
    # 有区分度对照：另一 agent 配置未登记 → fail 且指引带脚本名
    empty_repo = tmp_path / "repo2"
    (empty_repo / "config" / "agents" / "main").mkdir(parents=True)
    (empty_repo / "config" / "agents" / "main" / "agentos.yaml").write_text(
        "tool_ids: [unrelated]\n", encoding="utf-8"
    )
    missing = plugin_doctor.check_tool_ids(plugin_dir, empty_repo, manifest)
    assert not missing["pass"]
    assert "check_agent_tool_ids" in missing["detail"]


def test_doctor_verdict_healthy_for_empty_tools(tmp_path: Path) -> None:
    plugin_dir = _write_plugin(tmp_path, [])  # 功能性插件形态：tools==[]
    report = plugin_doctor.doctor(plugin_dir, repo_root=tmp_path)
    assert report["verdict"] == "healthy"


# ── debug_tool ───────────────────────────────────────────────────────────

def test_debug_tool_calls_http_handle_via_server_module(tmp_path: Path) -> None:
    theme_dir = _make(tmp_path, "theme", "dbg_theme")
    report = debug_tool.call_tool(
        theme_dir, "http.handle",
        {"path": "/ext/dbg_theme/styles/theme.css", "method": "GET"},
    )
    assert report["result"]["status"] == 200
    assert report["result"]["body_encoding"] == "base64"


def test_debug_tool_args_mismatch_raises(tmp_path: Path) -> None:
    theme_dir = _make(tmp_path, "theme", "dbg_theme2")
    with pytest.raises(debug_tool.DebugError, match="入参不匹配"):
        debug_tool.call_tool(theme_dir, "http.handle", {"bogus": 1})


# ── spawn_test_kernel.kernel_bin（装机自举车道） ─────────────────────────

def test_spawn_uses_kernel_bin_bypassing_repo_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kernel_bin 直指时不再按 workspace 推导仓内产物（装机同款二进制）。"""
    calls: list[Path] = []
    monkeypatch.setattr(
        spawn_test_kernel, "resolve_kernel_binary",
        lambda workspace: calls.append(workspace) or tmp_path / "should_not_be_used",
    )
    def _fake_popen(*_args: object, **_kwargs: object) -> None:
        raise spawn_test_kernel.TestKernelError("stub stop")

    monkeypatch.setattr(spawn_test_kernel.subprocess, "Popen", _fake_popen)
    fake_bin = tmp_path / "resources" / "kernel" / "agentos-kernel.exe"
    fake_bin.parent.mkdir(parents=True)
    fake_bin.write_text("", encoding="utf-8")
    with pytest.raises(spawn_test_kernel.TestKernelError):
        spawn_test_kernel.spawn(tmp_path, kernel_bin=str(fake_bin))
    assert calls == []  # 直指模式未触碰仓内产物解析


# ── 审查修复回归（2026-11-03 自审四疑点） ─────────────────────────────────

def test_scaffold_rejects_invalid_page_id(tmp_path: Path) -> None:
    """page_id 进入端点路径声明，非法字符必须拦截（审查实锤：Evil/../Path 曾产出坏路由）。"""
    with pytest.raises(scaffold_functional.ScaffoldError, match="page id 非法"):
        scaffold_functional.scaffold_functional(
            tmp_path, "ok_page", "page", "x", page_id="Evil/../Path"
        )


def test_spawn_kernel_bin_missing_raises_test_kernel_error(tmp_path: Path) -> None:
    """kernel_bin 指向不存在路径 → TestKernelError（非裸 FileNotFoundError）。"""
    with pytest.raises(spawn_test_kernel.TestKernelError, match="内核 exe 不存在"):
        spawn_test_kernel.spawn(tmp_path, kernel_bin=str(tmp_path / "no" / "such.exe"))


def test_doctor_instance_unreachable_reports_fail(tmp_path: Path) -> None:
    """查⑤实例不可达 → fail 且 detail 含指引（实测 URLError except 分支命中）。"""
    plugin_dir = _write_plugin(tmp_path, [])
    report = plugin_doctor.doctor(plugin_dir, repo_root=tmp_path, base_url="http://127.0.0.1:1")
    instance = next(c for c in report["checks"] if c["name"] == "instance")
    assert not instance["pass"]
    assert "实例不可达" in instance["detail"]


def test_generated_handler_test_skips_without_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SDK 不可达时生成物 handler 测试显式 skip（非 ImportError error）。

    importorskip 经 __import__ 解析；sys.modules 注入 None 使 import 抛
    ImportError，等价干净环境语义。
    """
    page_dir = _make(tmp_path, "page", "skip_sdk")
    monkeypatch.setitem(sys.modules, "agentos_plugin_sdk", None)
    spec = importlib.util.spec_from_file_location(
        "skip_sdk_generated", page_dir / "test_skip_sdk.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(pytest.skip.Exception):
        module.test_handle_returns_html_envelope()
    # 不依赖 SDK 的断言照常可跑
    module.test_page_html_self_contained()
    module.test_manifest_page_and_endpoint_declared()


# ── 第二刀：snippet_box 实测缺陷修复回归（认证/插件根语义/env_extra） ──────

def test_http_get_json_401_raises_token_hint(tmp_path: Path) -> None:
    """401 → VerifyError 且文案指明 token（缺陷①修复：匿名必 401 显式化）。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Always401(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Always401)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        with pytest.raises(verify_plugin.VerifyError, match="token"):
            verify_plugin.http_get_json(f"http://127.0.0.1:{port}/api/v1/tools")
    finally:
        server.shutdown()


def test_login_token_missing_password_raises(tmp_path: Path) -> None:
    """登录端点不可达 → VerifyError（fail-visible，不静默返回空 token）。"""
    with pytest.raises(verify_plugin.VerifyError, match="登录失败"):
        verify_plugin.login_token("http://127.0.0.1:1", "pw")


def test_build_kernel_env_merges_env_extra(tmp_path: Path) -> None:
    """env_extra 合并进内核环境（AGENTOS_USER_PLUGINS_DIR 承载面）。"""
    env = spawn_test_kernel.build_kernel_env(
        19999, tmp_path / "k.db", "pw", base={"AGENTOS_USER_PLUGINS_DIR": str(tmp_path)}
    )
    assert env["AGENTOS_USER_PLUGINS_DIR"] == str(tmp_path)
    assert env["AGENTOS_KERNEL_PORT"] == "19999"
    assert env["AGENTOS_ADMIN_PASSWORD"] == "pw"
