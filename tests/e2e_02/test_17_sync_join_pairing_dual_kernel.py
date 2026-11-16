# @feature: FP-0.2.多端同步 登录即入网 | @ci: none-local
"""登录即入网双内核验收车道（ADR 2026-11-02-login-as-enrollment，替代入网码配对流）。

同机起 D/B 两个真实内核实例（一次性 DB/端口/口令，各自独立 USER_ROOT，
**零预置 token**——不设 AGENTOS_SYNC_RELAY_TOKENS/AGENTOS_SYNC_TOKEN/
AGENTOS_SYNC_RELAY_URL，全走登录直签发流）：D=desktop（relay 宿主，启动时
sync_relay 以用户层 profile 预置为禁用）、B=phone。验收：

①一键启用语义保持：sync_relay 停用态端点拒达 → PUT enabled 热启用 → /enroll
  即时可达（开关状态可查）；
②登录直入网：D 以自身 admin JWT 经本机 sync_agent /enroll 直签自入网 → B 以
  **D 的 admin JWT**（信任锚 = relay 宿主内核的登录态，e2e 直登 D 即前端
  「已存会话」的等价物）经本机 sync_agent 代理直签入网；签发即注册入设备注册表；
③fail-closed：伪造 admin JWT 打 /enroll 拒收；缺 admin_jwt/垃圾入网体 400 零
  副作用；无 env 预置下外部伪造 device token 仍 403；
④双向收敛：D 改配置 → B 收敛；B 改配置 → D 收敛（HLC-LWW）。

门控：``AGENTOS_GS1=1``（与 G-S1 同族车道；G-S1 五场景回归 = test_16 同门实跑）。
实跑：``AGENTOS_GS1=1 python -m pytest tests/e2e_02/test_16_sync_gs1_dual_kernel.py tests/e2e_02/test_17_sync_join_pairing_dual_kernel.py -v``
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

import pytest
from test_16_sync_gs1_dual_kernel import (  # 同族车道共享装配（复制必漂移故复用）
    ADMIN_PASSWORD,
    KERNEL_BIN,
    ROOT,
    KernelInstance,
    _login_admin,
    _poll_until,
    _read,
    _settle_sync_agent,
    _write,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("AGENTOS_GS1") != "1",
        reason="登录入网双内核车道：设 AGENTOS_GS1=1 实跑（真实内核进程 ×2，"
        "与 G-S1 同族；直签发/动态 token 为进程内回归面）",
    ),
]

PORT_D, PORT_B = 19293, 19294


class EnrollKernelInstance(KernelInstance):
    """登录入网流实例：清零 G-S1 配方的手工 token/relay env（env() 基座后置覆盖）。

    入网流验收的先决条件 = 零预置凭据（不设 AGENTOS_SYNC_RELAY_TOKENS/
    AGENTOS_SYNC_TOKEN/AGENTOS_SYNC_RELAY_URL）；配方 env() 的基座注入晚于
    extra_env，故以子类在 final merge 后再覆盖。
    """

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        merged = dict(extra or {})
        merged.update({"AGENTOS_SYNC_TOKEN": "", "AGENTOS_SYNC_RELAY_URL": ""})
        return super().env(merged)

_BOOT_TIMEOUT_SECS = 300.0
_CONVERGE_TIMEOUT_SECS = 120.0

ADMIN = {"username": "admin", "password": ADMIN_PASSWORD}


def _relay_base(port: int) -> str:
    return f"http://127.0.0.1:{port}/ext/sync_relay"


def _http_any(
    method: str, url: str, token: str | None = None, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """容错 HTTP：响应体非 JSON（禁用路由的纯文本 404 等）解出原文本，不炸收集。"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, raw
    try:
        return 200, json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return 200, raw


def _kernel_get(instance: KernelInstance, path: str, token: str | None = None) -> tuple[int, Any]:
    return _http_any("GET", f"{instance.base_url}{path}", token)


def _kernel_post(
    instance: KernelInstance, path: str, body: dict[str, Any] | None, token: str | None = None
) -> tuple[int, Any]:
    return _http_any("POST", f"{instance.base_url}{path}", token, body)


def _relay_direct(
    port: int, path: str, body: dict[str, Any], bearer: str | None = None
) -> tuple[int, Any]:
    """直连 relay 端点（内核 admin 执法面的原始调用者视角，不带合法 JWT）。"""
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _seed_profile_relay_disabled(instance: KernelInstance) -> None:
    """用户层 profile 预置 sync_relay 禁用（文件级整体替换 → 复制出厂全文再改）。"""
    factory = (ROOT / "config" / "kernel" / "default_profile.yaml").read_text(encoding="utf-8")
    assert "sync_relay" not in factory, "出厂 profile 已含 sync_relay 条目时本播种需同步调整"
    patched = factory.replace("defaults:", "  sync_relay:\n    enabled: false\ndefaults:", 1)
    assert patched != factory, "出厂 profile 缺 defaults 锚，播种失败"
    target = instance.config_dir / "kernel" / "default_profile.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(patched, encoding="utf-8")


def _relay_enabled(instance: KernelInstance, token: str) -> bool | None:
    status, body = _kernel_get(instance, "/api/v1/plugins", token)
    assert status == 200, f"插件列表查询失败: {status}"
    plugins = body.get("plugins", []) if isinstance(body, dict) else body
    for plugin in plugins:
        if isinstance(plugin, dict) and plugin.get("plugin_id") == "sync_relay":
            return bool(plugin.get("enabled"))
    return None


def _put_relay_enabled(instance: KernelInstance, token: str, enabled: bool) -> tuple[int, Any]:
    return _http_any(
        "PUT", f"{instance.base_url}/api/v1/plugins/sync_relay/enabled", token, {"enabled": enabled}
    )


def _agent_status(instance: KernelInstance, token: str) -> dict[str, Any]:
    status, body = _kernel_get(instance, "/ext/sync_agent/status", token)
    assert status == 200, f"sync_agent status 查询失败: {status} {body}"
    return body


def _enroll_via_agent(
    instance: KernelInstance, relay_url: str, admin_jwt: str, token: str
) -> tuple[int, Any]:
    """登录直入网走本机内核 admin 面（产品流 = 已登录前端编排，携本机内核 JWT）。"""
    return _kernel_post(
        instance,
        "/ext/sync_agent/enroll",
        {"relay_url": relay_url, "admin_jwt": admin_jwt},
        token,
    )


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """D/B 两内核实例（module 级：一次起停，场景顺序共用）。"""
    if not KERNEL_BIN.is_file():
        pytest.fail(f"内核二进制缺失: {KERNEL_BIN}（先 cargo build --release）")
    root = tmp_path_factory.mktemp("login_enroll")
    desktop = EnrollKernelInstance("desktop-a", PORT_D, root / "d")
    phone = EnrollKernelInstance("phone-b", PORT_B, root / "b")
    _seed_profile_relay_disabled(desktop)
    for instance in (desktop, phone):
        instance.start()
    try:
        for instance in (desktop, phone):
            instance.wait_healthy()
            _settle_sync_agent(instance)
        yield {"desktop": desktop, "phone": phone}
    finally:
        for instance in (desktop, phone):
            instance.kill_tree()


def test_10_switch_enables_relay_hot(fleet: dict[str, Any]) -> None:
    """场景①：开关一键启用——禁用态端点拒达 → PUT enabled → /enroll 即时可达。"""
    desktop = fleet["desktop"]
    token = _login_admin(desktop)

    assert _relay_enabled(desktop, token) is False, "预置 profile：sync_relay 启动即禁用"
    status, _ = _kernel_post(
        desktop,
        "/ext/sync_relay/enroll",
        {"device_id": "probe", "tier": "phone", "app_version": "x"},
        token,
    )
    assert status != 200, "禁用态 /enroll 不应可达（路由未注册）"

    status, body = _put_relay_enabled(desktop, token, True)
    assert status == 200, f"开关启用失败: {status} {body}"
    assert body.get("success") is True, f"开关启用失败: {body}"

    deadline = time.monotonic() + 60.0
    last_status = 0
    while time.monotonic() < deadline:
        if _relay_enabled(desktop, token) is True:
            try:
                # 可达性探针：缺 device_id 的入网体 = 路由已注册（handler 400），
                # 不产生设备登记（零副作用的可达性证明）。
                last_status, _ = _kernel_post(
                    desktop, "/ext/sync_relay/enroll", {"tier": "phone"}, token
                )
                if last_status == 400:
                    break
            except (ConnectionError, OSError):
                last_status = -1
        time.sleep(1.0)
    assert _relay_enabled(desktop, token) is True, "开关状态可见（插件列表 enabled 翻转）"
    assert last_status == 400, f"启用后 /enroll 即时可达（热注册，缺字段 400）: {last_status}"
    status, body = _kernel_get(desktop, "/ext/sync_relay/devices", token)
    assert all(
        d["device_id"] != "probe" for d in body.get("devices", [])
    ), "可达性探针零副作用（不产生设备登记）"


def test_20_login_enroll_both_devices(fleet: dict[str, Any]) -> None:
    """场景②：登录直入网——D 自签自入网 + B 携 D 的 admin JWT 代理直签入网。"""
    desktop, phone = fleet["desktop"], fleet["phone"]
    token_d = _login_admin(desktop)
    token_b = _login_admin(phone)

    # 入网前：两端 sync_agent 未配对（零 env 预置 → 引擎停转、状态可查）。
    for instance, name in ((desktop, "desktop"), (phone, "phone")):
        before = _agent_status(instance, _login_admin(instance))
        assert before["paired"] is False, f"{name} 入网前应为未配对，实得 {before}"

    # D 自入网：本机 admin JWT（本机闸）+ 自身即 relay 宿主（relay JWT 同一枚）。
    status, body = _enroll_via_agent(desktop, _relay_base(PORT_D), token_d, token_d)
    assert status == 200, f"D 自入网失败: {status} {body}"
    assert body.get("paired") is True, f"D 自入网失败: {body}"
    assert _agent_status(desktop, token_d)["paired"] is True, "D 入网状态即时可见"

    # B 入网：relay 宿主的 admin JWT 即凭据锚（= 前端「已存会话」换发的等价物），
    # 经 B 本机 sync_agent 出站直签。
    status, body = _enroll_via_agent(phone, _relay_base(PORT_D), token_d, token_b)
    assert status == 200, f"B 入网失败: {status} {body}"
    assert body.get("paired") is True, f"B 入网失败: {body}"
    assert _agent_status(phone, token_b)["paired"] is True

    status, body = _kernel_get(desktop, "/ext/sync_relay/devices", token_d)
    assert status == 200
    rows = {d["device_id"]: d for d in body["devices"]}
    assert "desktop-a" in rows, f"直签发即注册: {rows}"
    assert "phone-b" in rows, f"直签发即注册: {rows}"
    assert rows["desktop-a"]["tier"] == "desktop"
    assert rows["phone-b"]["tier"] == "phone"


def test_30_enroll_fail_closed_no_side_effect(fleet: dict[str, Any]) -> None:
    """场景③：fail-closed——伪造 admin JWT 拒收；缺字段/垃圾体 400；伪造 device token 403。"""
    desktop, phone = fleet["desktop"], fleet["phone"]
    token_d = _login_admin(desktop)

    # 伪造 admin JWT 直打 relay /enroll：内核 admin 执法面拒收（fail-closed）。
    status, body = _relay_direct(
        PORT_D,
        "/ext/sync_relay/enroll",
        {"device_id": "ghost", "tier": "phone", "app_version": "x"},
        bearer="forged-jwt",
    )
    assert status in (401, 403), f"伪造 admin JWT 必须拒收: {status} {body}"

    # 缺 admin_jwt / 垃圾入网体：本机闸 400，既有配对不破坏。
    status, body = _kernel_post(phone, "/ext/sync_agent/enroll", {"relay_url": _relay_base(PORT_D)}, token_d)
    assert status == 400, f"缺 admin_jwt 400: {status} {body}"
    status, body = _kernel_post(phone, "/ext/sync_agent/enroll", {"admin_jwt": "jwt"}, token_d)
    assert status == 400, f"缺 relay_url 400: {status} {body}"
    assert _agent_status(phone, _login_admin(phone))["paired"] is True, "垃圾入网体不破坏既有配对"

    # 无预置 token 下伪造 device token 仍 fail-closed。
    status, body = _relay_direct(
        PORT_D, "/ext/sync_relay/events", {"events": []}, bearer="forged-token"
    )
    assert status in (401, 403), f"无预置 token 下伪造凭据必须 fail-closed: {status} {body}"

    status, body = _kernel_get(desktop, "/ext/sync_relay/devices", token_d)
    rows = {d["device_id"] for d in body["devices"]}
    assert "ghost" not in rows, "拒收不产生设备登记（零副作用）"


def test_40_bidirectional_convergence_hlc_lww(fleet: dict[str, Any]) -> None:
    """场景④：入网后双向收敛——D 改配置到 B；B 改配置到 D（HLC-LWW）。"""
    desktop, phone = fleet["desktop"], fleet["phone"]

    _write(desktop, "from-desktop.yaml", "side: desktop\n")

    def phone_got_desktop() -> object:
        return _read(phone, "from-desktop.yaml") == "side: desktop\n"

    _poll_until("B 收敛 D 的配置", phone_got_desktop, _CONVERGE_TIMEOUT_SECS)

    _write(phone, "from-phone.yaml", "side: phone\n")

    def desktop_got_phone() -> object:
        return _read(desktop, "from-phone.yaml") == "side: phone\n"

    _poll_until("D 收敛 B 的配置", desktop_got_phone, _CONVERGE_TIMEOUT_SECS)
