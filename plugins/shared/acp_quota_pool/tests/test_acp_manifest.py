# @feature: FP-0.2.ACP额度池 R6网关托管 | @ci: python-coverage
"""插件页/端点/配置声明契约测试（manifest 数据面，不 import server）。

断言三组一致性：contributes.pages 的 webview 页声明形态（与前端渲染消费
契约对齐）、http_endpoints 三路由（页面 JS 引用的路径与声明互证防漂移）、
config_files[gateway] 与仓库真值文件（存在性 + 密钥引用形态：只允许环境
变量名，真值禁入仓）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import acp_console_page
import pytest
import yaml
from acp_gateway import MANAGEMENT_PAGE

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _PLUGIN_DIR.parents[2]
_GATEWAY_YAML = _REPO_ROOT / "config/plugins/acp_quota_pool/gateway.yaml"


def _manifest() -> dict:
    return json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))


def _gateway_truth() -> dict:
    return yaml.safe_load(_GATEWAY_YAML.read_text(encoding="utf-8"))


def _endpoints_by_route() -> dict[str, dict]:
    return {ep["route_id"]: ep for ep in _manifest()["http_endpoints"]}


# ── 插件页声明（webview 直嵌形态）──────────────────────────────────────────


def test_console_page_declared_as_webview_tab_page() -> None:
    pages = {p["id"]: p for p in _manifest()["contributes"]["pages"]}
    page = pages["acp_gateway_console"]
    assert page["widget"] == "webview"
    assert page["slot"] == "tab"
    assert page["path"] == "/p/acp_gateway_console"
    assert page["props"]["pluginId"] == _manifest()["id"]
    assert page["props"]["widgetId"] == "acp_gateway_console"


def test_console_page_htmlpath_maps_to_declared_endpoint() -> None:
    html_path = _manifest()["contributes"]["pages"][0]["props"]["htmlPath"]
    endpoint = _endpoints_by_route()["acp_console_page"]
    assert endpoint["path"] == f"/ext/{_manifest()['id']}{html_path}"
    assert endpoint["method"] == "GET"
    assert endpoint["handler_capability"] == "http.handle"


def test_console_page_html_references_status_and_start_endpoints() -> None:
    html = acp_console_page.CONSOLE_HTML
    assert "/ext/acp_quota_pool/gateway/status" in html
    # 2026-11-21 拉起面单点化：页面不再调 start 端点（拉起由 llm_provider_gateway
    # 插件承接）——未运行态显式移交提示 + 重新检查；start 调用面不得回归。
    assert "/ext/acp_quota_pool/gateway/start" not in html
    assert "llm_provider_gateway" in html
    # 运行态卡片 + 新窗口打开管理页（window.open 走沙箱 allow-popups，真实
    # origin）形态守卫。c08ffeae1 起废除 iframe 自导航 location.replace——
    # React SPA 强依赖 storage，沙箱 opaque origin 下 SecurityError 白屏
    # （用户实测实证），不得回归。
    assert "window.open" in html
    assert "location.replace" not in html
    assert "拉起网关" not in html  # 拉起按钮语义退役
    assert MANAGEMENT_PAGE in html or "management_url" in html


def test_gateway_status_and_start_routes_contract() -> None:
    endpoints = _endpoints_by_route()
    assert endpoints["acp_gateway_status"]["path"] == "/ext/acp_quota_pool/gateway/status"
    assert endpoints["acp_gateway_status"]["method"] == "GET"
    assert endpoints["acp_gateway_start"]["path"] == "/ext/acp_quota_pool/gateway/start"
    assert endpoints["acp_gateway_start"]["method"] == "POST"
    for route_id in ("acp_gateway_status", "acp_gateway_start", "acp_console_page"):
        assert endpoints[route_id]["handler_capability"] == "http.handle"


# ── gateway 配置（真值文件 + 密钥引用形态）─────────────────────────────────


def test_gateway_config_file_declared_and_present() -> None:
    config_files = {c["id"]: c for c in _manifest()["config_files"]}
    declared_path = config_files["gateway"]["path"]
    assert (_REPO_ROOT / declared_path).is_file(), f"配置真值缺失: {declared_path}"


def test_gateway_config_truth_shape() -> None:
    truth = _gateway_truth()
    assert truth["base_url"].startswith("http")
    # 2026-11-21 拉起面单点化：start_command_* 拉起命令面（含个人路径硬编码）
    # 全量退役——真值文件不得再含任何拉起命令键（探活面零拉起）。
    for retired in ("start_command", "start_command_windows", "start_command_linux", "start_command_darwin"):
        assert retired not in truth, f"退役拉起键残留: {retired}"
    assert float(truth["health_timeout_secs"]) > 0


def test_gateway_truth_carries_no_personal_paths() -> None:
    """个人路径红线：真值文件不得含本机个人目录（装机态不可用面）。"""
    text = _GATEWAY_YAML.read_text(encoding="utf-8")
    assert "C:\\Users\\jc" not in text
    assert "start-gateway.bat" not in text


def test_gateway_manifest_declares_probe_only_fields() -> None:
    fields = {
        field["name"]
        for config_file in _manifest()["config_files"]
        if config_file["id"] == "gateway"
        for field in config_file.get("fields", [])
    }
    assert fields == {"base_url", "health_timeout_secs"}


def test_gateway_config_management_key_is_reference_not_truth() -> None:
    # 密钥纪律：文件里只允许环境变量名引用（大写+下划线形态），真值禁入仓
    truth = _gateway_truth()
    assert re.fullmatch(r"[A-Z][A-Z0-9_]*", str(truth["management_key_env"]))
    assert set(truth).isdisjoint({"secret", "secret_key", "management_key"})


@pytest.mark.parametrize(
    "permission_key",
    ["ACP_GATEWAY_BASE_URL"],
)
def test_gateway_env_overrides_declared_in_permissions(permission_key: str) -> None:
    assert permission_key in _manifest()["permissions"]["env_vars"]


def test_retired_start_command_env_not_declared() -> None:
    """退役面钉子：拉起命令环境变量不再出现在权限声明面。"""
    assert "ACP_GATEWAY_START_COMMAND" not in _manifest()["permissions"]["env_vars"]
