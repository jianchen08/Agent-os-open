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
- spawn_test_kernel.spawn：kernel_bin 直指时绕过仓内产物解析（装机自举车道）；
- verify_plugin 三根验证实例（ADR 2026-11-07 跟刀）：Tier2 走干净视图、
  Tier1 走全量 Tier1 视图、运行时副本断言防同 id 旧副本假绿；
- verify_host 宿主编排：后端 keep_running 实例被前端探针复用、统一 stop、
  合并证据（WSL interop 命令转发的宿主侧入口）。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "plugin-dev" / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[1]
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
verify_host = _load("verify_host")

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
    """env_extra 合并进内核环境（显式调用方变量最后赢）。"""
    env = spawn_test_kernel.build_kernel_env(
        19999, tmp_path / "k.db", "pw", base={"AGENTOS_USER_PLUGINS_DIR": str(tmp_path)}
    )
    assert env["AGENTOS_USER_PLUGINS_DIR"] == str(tmp_path)
    assert env["AGENTOS_KERNEL_PORT"] == "19999"
    assert env["AGENTOS_ADMIN_PASSWORD"] == "pw"


# ── 第三刀：分级单副本跟刀（三根验证实例 + 假绿防线，2026-11-11） ─────────

def _fake_runtime(tmp_path: Path) -> tuple[Path, Path]:
    """假仓库形态：config/kernel/plugin_tiers.yaml + plugins/shared（Tier1+共享件）。"""
    workspace = tmp_path / "repo"
    kernel_cfg = workspace / "config" / "kernel"
    kernel_cfg.mkdir(parents=True)
    (kernel_cfg / "plugin_tiers.yaml").write_text(
        "tier1:\n"
        "  safety_plugins:\n"
        "    - approval_service\n"
        "  shared_modules:\n"
        "    - _libs\n",
        encoding="utf-8",
    )
    base = workspace / "plugins" / "shared"
    (base / "_libs").mkdir(parents=True)
    approval = base / "approval"
    approval.mkdir()
    (approval / "plugin.json").write_text(
        json.dumps({"id": "approval_service", "version": "1.0.0"}),
        encoding="utf-8",
    )
    return workspace, base


def _staging_plugin(parent: Path, plugin_id: str, version: str = "0.1.0") -> Path:
    plugin_dir = parent / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({
            "id": plugin_id, "version": version,
            "capabilities": {"tools": []},
        }),
        encoding="utf-8",
    )
    return plugin_dir


def test_resolve_config_root_kernel_bin_resources_layout(tmp_path: Path) -> None:
    """装机形态：kernel_bin 在 <install>/resources/kernel/ 下 → 配置根取同级 config。"""
    workspace, _ = _fake_runtime(tmp_path)
    kernel_bin = tmp_path / "install" / "resources" / "kernel" / "agentos-kernel.exe"
    kernel_bin.parent.mkdir(parents=True)
    kernel_bin.write_text("", encoding="utf-8")
    resources_config = tmp_path / "install" / "resources" / "config"
    (resources_config / "kernel").mkdir(parents=True)
    (resources_config / "kernel" / "plugin_tiers.yaml").write_text(
        "tier1:\n  safety_plugins:\n    - approval_service\n", encoding="utf-8"
    )
    assert verify_plugin.resolve_config_root(workspace, kernel_bin=str(kernel_bin)) == resources_config.resolve()


def test_resolve_config_root_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(verify_plugin.VerifyError, match="plugin_tiers.yaml"):
        verify_plugin.resolve_config_root(tmp_path / "empty")


def test_read_tier1_ids_reads_safety_plugins(tmp_path: Path) -> None:
    workspace, _ = _fake_runtime(tmp_path)
    assert verify_plugin.read_tier1_ids(workspace / "config") == {"approval_service"}


def test_build_instance_config_tier2_routes_to_clean_view(tmp_path: Path) -> None:
    """Tier2：tier1/shared 指内置根，tier2 指只含被测插件的干净视图。"""
    workspace, base = _fake_runtime(tmp_path)
    staged = _staging_plugin(tmp_path / "staging", "demo_tool")
    work = tmp_path / "work"
    work.mkdir()
    cfg = verify_plugin.build_instance_config(
        staged, "demo_tool", base, workspace / "config", work
    )
    assert cfg["tier"] == "tier2"
    assert cfg["tier1_root"] == base
    assert cfg["shared_root"] == base
    assert cfg["tier2_root"] == work / ".verify_tier2_view"
    assert (cfg["tier2_root"] / "demo_tool" / "plugin.json").is_file()
    data = yaml.safe_load(cfg["path"].read_text(encoding="utf-8"))
    assert data == {
        "schema": 1,
        "tier1_root": str(base),
        "tier2_root": str(work / ".verify_tier2_view"),
        "shared_root": str(base),
    }


def test_build_instance_config_tier1_view_carries_all_tier1(tmp_path: Path) -> None:
    """Tier1：视图必须含清单内全部 Tier1（守卫拒启缺席），被测件替换为 staging。"""
    workspace, base = _fake_runtime(tmp_path)
    staged = _staging_plugin(tmp_path / "staging", "approval_service", version="1.0.1")
    work = tmp_path / "work"
    work.mkdir()
    cfg = verify_plugin.build_instance_config(
        staged, "approval_service", base, workspace / "config", work
    )
    assert cfg["tier"] == "tier1"
    view = cfg["tier1_root"]
    assert (view / "approval" / "plugin.json").is_file()
    runtime_manifest = json.loads(
        (view / "approval" / "plugin.json").read_text(encoding="utf-8")
    )
    assert runtime_manifest["version"] == "1.0.1"  # 指向 staging 而非内置旧版
    assert cfg["tier2_root"].is_dir()
    assert list(cfg["tier2_root"].iterdir()) == []


def test_loaded_copy_rejects_foreign_copy_and_accepts_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """假绿防线：同 id 旧副本（内置根路径）必须 fail，视图内路径 + 版本一致才 pass。"""
    view = tmp_path / "view"
    (view / "demo_tool").mkdir(parents=True)
    foreign = tmp_path / "repo" / "plugins" / "shared" / "demo_tool" / "plugin.json"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("{}", encoding="utf-8")

    def _payload(path: Path, version: str) -> dict:
        return {"manifest": {"plugins": [{
            "plugin_id": "demo_tool", "version": version, "tier": "tier2",
            "path": str(path), "source_root": "tier1",
        }]}}

    def _fake_get(payload: dict):
        def _inner(*_args: object, **_kwargs: object) -> dict:
            return payload
        return _inner

    monkeypatch.setattr(
        verify_plugin, "http_get_json", _fake_get(_payload(foreign, "0.1.0"))
    )
    foreign_check = verify_plugin.check_loaded_copy(
        "http://x", "demo_tool", "0.1.0", view
    )
    assert foreign_check["pass"] is False

    staged_path = view / "demo_tool" / "plugin.json"
    monkeypatch.setattr(
        verify_plugin, "http_get_json", _fake_get(_payload(staged_path, "0.1.0"))
    )
    staged_check = verify_plugin.check_loaded_copy(
        "http://x", "demo_tool", "0.1.0", view
    )
    assert staged_check["pass"] is True

    # 路径对但版本不符（视图里不是本次 staging 的版本）同样 fail
    monkeypatch.setattr(
        verify_plugin, "http_get_json", _fake_get(_payload(staged_path, "0.0.9"))
    )
    stale_version = verify_plugin.check_loaded_copy(
        "http://x", "demo_tool", "0.1.0", view
    )
    assert stale_version["pass"] is False


def test_path_is_under_keeps_junction_view_path(tmp_path: Path) -> None:
    """视图 junction 的运行时路径按视图归属判（resolve 会把链接还原成 staging 真身）。"""
    real = tmp_path / "staging" / "demo_tool"
    real.mkdir(parents=True)
    view = tmp_path / "view"
    view.mkdir()
    verify_plugin._make_junction(view / "demo_tool", real)
    assert verify_plugin._path_is_under(str(view / "demo_tool"), view) is True
    assert verify_plugin._path_is_under(str(view / "demo_tool"), real) is False


def test_check_tools_visible_empty_is_not_applicable() -> None:
    """功能性插件（tools==[]）工具面断言不适用——不应因「0 个工具」判红。"""
    check = verify_plugin.check_tools_visible("http://127.0.0.1:1", [])
    assert check["pass"] is True
    assert "不适用" in check["detail"]


def test_login_tokens_returns_access_and_refresh() -> None:
    """登录响应解析出 refresh_token（浏览器车道注入 localStorage 的凭据）。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Login(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps({
                "access_token": "acc-123",
                "refresh_token": "ref-456",
                "token_type": "bearer",
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), _Login)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        tokens = verify_plugin.login_tokens(f"http://127.0.0.1:{port}", "pw")
    finally:
        server.shutdown()
        server.server_close()
    assert tokens == {"access_token": "acc-123", "refresh_token": "ref-456"}


# ── 宿主验证编排（命令转发 + 前端浏览器车道，2026-11-11） ─────────────────

def _fake_spawn_module(stop_calls: list[str]):
    class _Spawn:
        @staticmethod
        def spawn(*_args: object, **_kwargs: object) -> dict:
            return {
                "port": 43123, "run_id": "run_x",
                "admin_password": "pw", "scratch_root": "scratch",
            }

        @staticmethod
        def stop(scratch_dir: Path, run_id: str | None = None) -> dict:
            stop_calls.append(f"{scratch_dir}:{run_id}")
            return {"stopped": True}

    return _Spawn


def test_verify_plugin_keep_running_skips_cleanup(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """keep_running=True 保留实例交前端车道；默认路径必须 stop（防双停/泄漏）。"""
    staging = _staging_plugin(tmp_path / "staging", "demo_tool")
    stop_calls: list[str] = []
    fake_spawn = _fake_spawn_module(stop_calls)
    instance_cfg = {
        "tier": "tier2",
        "path": tmp_path / "instance.yaml",
        "tier1_root": tmp_path / "base",
        "tier2_root": tmp_path / "view",
        "shared_root": tmp_path / "base",
        "expected_runtime_root": tmp_path / "view",
    }
    work_dirs: list[Path] = []

    def _fake_build(*args: object, **_kwargs: object) -> dict:
        work_dirs.append(Path(str(args[4])))
        return instance_cfg

    monkeypatch.setattr(verify_plugin, "_load_spawn_test_kernel", lambda: fake_spawn)
    monkeypatch.setattr(verify_plugin, "resolve_config_root", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(verify_plugin, "resolve_base_plugins_root", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(verify_plugin, "build_instance_config", _fake_build)
    monkeypatch.setattr(
        verify_plugin, "login_tokens",
        lambda *_a, **_k: {"access_token": "t", "refresh_token": "r"},
    )
    monkeypatch.setattr(
        verify_plugin, "check_tools_visible",
        lambda *_a, **_k: {"name": "tools_visible", "pass": True, "detail": "stub"},
    )
    monkeypatch.setattr(
        verify_plugin, "check_loaded_copy",
        lambda *_a, **_k: {"name": "loaded_copy", "pass": True, "detail": "stub"},
    )
    monkeypatch.setattr(
        verify_plugin, "check_schema_declared",
        lambda *_a, **_k: {"name": "schema_declared", "pass": True, "detail": "stub"},
    )

    kept = verify_plugin.verify_plugin(staging, tmp_path, keep_running=True)
    assert kept["instance"]["port"] == 43123
    assert kept["instance"]["admin_password"] == "pw"  # 内存里保留给前端车道
    disk = json.loads((staging / "verify_evidence.json").read_text(encoding="utf-8"))
    assert disk["instance"]["admin_password"] == "***"  # 落盘必须脱敏
    assert stop_calls == []
    # WSL 转发形态：插件路径可能是相对值——instance.yaml 三根必须已钉绝对路径
    # （相对值会被内核按配置文件目录二次拼接，启动守卫直接拒启，2026-11-11 实测）
    monkeypatch.chdir(tmp_path)
    verify_plugin.verify_plugin(Path("staging") / "demo_tool", tmp_path, keep_running=True)
    assert work_dirs
    assert all(path.is_absolute() for path in work_dirs)

    stopped = verify_plugin.verify_plugin(staging, tmp_path)
    assert stop_calls == [f"{staging.parent / '.verify_scratch'}:run_x"]
    assert "instance" not in stopped


def test_resolve_frontend_dist_prefers_kernel_bin_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """装机形态：kernel_bin 在 resources/kernel 下 → 前端 dist 取 resources/frontend/dist。"""
    monkeypatch.setattr(verify_host, "_REPO_ROOT", tmp_path / "no_repo")
    workspace = tmp_path / "repo"
    (workspace / "frontend" / "dist").mkdir(parents=True)
    (workspace / "frontend" / "dist" / "index.html").write_text("<html/>", encoding="utf-8")
    kernel_bin = tmp_path / "install" / "resources" / "kernel" / "agentos-kernel.exe"
    kernel_bin.parent.mkdir(parents=True)
    kernel_bin.write_text("", encoding="utf-8")
    installed_dist = tmp_path / "install" / "resources" / "frontend" / "dist"
    installed_dist.mkdir(parents=True)
    (installed_dist / "index.html").write_text("<html/>", encoding="utf-8")
    resolved = verify_host.resolve_frontend_dist(workspace, str(kernel_bin))
    assert resolved == installed_dist.resolve()
    with pytest.raises(verify_host.HostVerifyError, match="index.html"):
        verify_host.resolve_frontend_dist(tmp_path / "empty")


def test_verify_host_single_instance_backend_then_frontend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """后端 keep_running 的实例被前端探针复用，最后统一 stop，证据合并落盘。"""
    plugin_dir = _staging_plugin(tmp_path / "staging", "demo_page")
    stop_calls: list[str] = []
    probe_calls: list[dict] = []
    probes: list[dict] = []

    class _FakeVP:
        VerifyError = verify_plugin.VerifyError

        @staticmethod
        def read_declared_tools(_plugin_dir: Path) -> dict:
            return {"id": "demo_page", "version": "0.1.0", "tools": []}

        @staticmethod
        def _load_spawn_test_kernel():
            return _fake_spawn_module(stop_calls)

        @staticmethod
        def redact_instance_secret(evidence: dict) -> dict:
            return verify_plugin.redact_instance_secret(evidence)

        @staticmethod
        def verify_plugin(*_a: object, **_k: object) -> dict:
            return {
                "plugin_id": "demo_page", "all_pass": True,
                "checks": [{"name": "tools_visible", "pass": True}],
                "instance": {
                    "port": 43123, "run_id": "run_x", "admin_password": "s3cret",
                    "scratch_root": str(tmp_path / "scratch"),
                },
            }

    def _fake_probe(plugin_dir: Path, workspace: Path, instance: dict,
                    dist_dir: Path, **_kwargs: object) -> dict:
        probe_calls.append({"plugin": plugin_dir, "instance": instance, "dist": dist_dir})
        if probes:
            return probes.pop(0)
        return {
            "checks": [{"name": "frontend_app_shell", "pass": True, "detail": "stub"}],
            "screenshots": ["shot.png"], "error": None,
        }

    monkeypatch.setattr(verify_host, "_load_verify_plugin", lambda: _FakeVP)
    monkeypatch.setattr(verify_host, "run_frontend_probe", _fake_probe)
    monkeypatch.setattr(
        verify_host, "resolve_frontend_dist", lambda *_a, **_k: tmp_path / "dist"
    )

    evidence = verify_host.verify_host(plugin_dir, tmp_path, frontend=True)
    assert evidence["all_pass"] is True
    assert probe_calls[0]["instance"]["port"] == 43123  # 复用后端实例
    assert stop_calls == [f"{tmp_path / 'scratch'}:run_x"]
    evidence_file = plugin_dir / "verify_host_evidence.json"
    assert evidence_file.is_file()
    assert "s3cret" not in json.dumps(evidence, ensure_ascii=False)
    assert "s3cret" not in evidence_file.read_text(encoding="utf-8")
    assert "***" in evidence_file.read_text(encoding="utf-8")
    frontend_check = next(c for c in evidence["checks"] if c["name"] == "frontend_app_shell")
    assert frontend_check["pass"] is True

    # 前端探针自身报错 → 合并证据记红（不吞）
    probes.append({"checks": [], "error": "probe boom", "screenshots": []})
    failed = verify_host.verify_host(plugin_dir, tmp_path, frontend=True)
    assert failed["all_pass"] is False
    probe_check = next(c for c in failed["checks"] if c["name"] == "frontend_probe")
    assert probe_check["pass"] is False


def test_run_frontend_probe_writes_config_and_reads_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探针配置交付内核地址/口令/dist，result.json 回读并带 exit_code。"""
    plugin_dir = _staging_plugin(tmp_path / "staging", "demo_page")
    captured: dict[str, object] = {}

    def _fake_run(cmd: list[str], **_kwargs: object):
        captured["cmd"] = cmd
        out_dir = plugin_dir / "verify_frontend_evidence"
        (out_dir / "result.json").write_text(
            json.dumps({"checks": [{"name": "frontend_app_shell", "pass": True}],
                        "screenshots": [], "error": None}),
            encoding="utf-8",
        )

        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Proc()

    monkeypatch.setattr(verify_host.subprocess, "run", _fake_run)
    monkeypatch.setattr(verify_host, "resolve_node", lambda _explicit=None: "node")

    result = verify_host.run_frontend_probe(
        plugin_dir, tmp_path, {"port": 43123, "admin_password": "pw"},
        tmp_path / "dist",
    )
    config = json.loads(
        (plugin_dir / "verify_frontend_evidence" / "probe_config.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["kernel_base"] == "http://127.0.0.1:43123"
    assert config["admin_password"] == "pw"
    assert config["dist_dir"] == str(tmp_path / "dist")
    assert result["exit_code"] == 0
    assert result["checks"][0]["name"] == "frontend_app_shell"
    assert captured["cmd"][0] == "node"


def test_spawn_delivers_instance_config_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spawn 具名参数落到内核环境（分级单副本三根载体）。"""
    captured: dict[str, str] = {}

    def _fake_popen(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs["env"])  # type: ignore[arg-type]
        raise spawn_test_kernel.TestKernelError("stub stop")

    monkeypatch.setattr(spawn_test_kernel.subprocess, "Popen", _fake_popen)
    fake_bin = tmp_path / "agentos-kernel.exe"
    fake_bin.write_text("", encoding="utf-8")
    with pytest.raises(spawn_test_kernel.TestKernelError):
        spawn_test_kernel.spawn(
            tmp_path, kernel_bin=str(fake_bin), instance_config="i.yaml",
            config_root="cfg", user_root="user",
        )
    assert captured["AGENTOS_INSTANCE_CONFIG"] == "i.yaml"
    assert captured["AGENTOS_CONFIG_ROOT"] == "cfg"
    assert captured["AGENTOS_USER_ROOT"] == "user"
