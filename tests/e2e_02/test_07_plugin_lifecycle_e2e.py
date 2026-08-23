"""
旅程 7：插件装卸载生命周期 e2e（真内核 + 真 sidecar，非 mock）

验证"插件即声明、装卸即时生效"的核心承诺，对象为专用探针插件
plugins/shared/tools/e2e_lifecycle_probe（默认 disabled）：

  7.1 装载生效：PUT enabled=true → 工具进 LLM 工具面（GET /api/v1/tools）
      + POST /ext/e2e_lifecycle_probe/echo 200 回声（真 sidecar 进程执行）
  7.2 禁用失效：PUT enabled=false → 工具立即从工具面消失 + /ext 404
  7.3 再启用对称：PUT enabled=true → 双面恢复
  7.4 卸载失效：插件目录摘除（watcher P1）→ 工具消失 + 插件从
      GET /api/v1/plugins 列表消失（store 摘除，无幽灵条目）
  7.5 重装启用：目录放回（热发现，disabled 入 store）→ PUT enabled=true
      → 双面恢复。该流程回归保护 2026-08-23 watcher 修复：修复前
      运行期发现的 disabled 插件 manifest 不进 store，PUT 启用静默不注册
      （"装了插件点启用功能不生效，须重启内核"）。

依赖：
  - Kernel 运行在 http://localhost:9100（e2e 车道标准环境）
  - 探针插件 .venv 已建（CI: e2e.yml uv sync per locked plugin）

数据卫生：全程只改探针插件自身状态与 plugins/shared/tools 目录名，
default_profile.yaml 在 teardown 恢复原始字节。
"""
from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from e2e_helpers import http_get, http_get_with_auth, http_post_json

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(420)]

PLUGIN_ID = "e2e_lifecycle_probe"
TOOL_NAME = "e2e_probe_echo"
PROBE_DIR = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "shared"
    / "tools"
    / "e2e_lifecycle_probe"
)
PROFILE_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "plugins" / "default_profile.yaml"
)
STASH_PARENT = Path(__file__).resolve().parents[2] / "logs"
STASH_DIR = STASH_PARENT / "e2e_lifecycle_probe_stash"
NUDGE_DIR = (
    Path(__file__).resolve().parents[2] / "plugins" / "shared" / "tools" / "_e2e_lifecycle_nudge"
)
# nudge 备选 rewrite 目标（探针目录被摘走时用；字节不变 → GAP-6 指纹不变）
SIMPLE_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "shared"
    / "tools"
    / "simple"
    / "plugin.json"
)


# ── 内核面轮询/操作工具 ────────────────────────────────────────────────


def _put_json_auth(url: str, token: str, data: dict, timeout: int = 30):
    """PUT JSON（admin 写面），返回 (status, body)。"""
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return e.code, body


def _tool_in_face(kernel: str) -> bool:
    """探针工具是否在 LLM 工具面（GET /api/v1/tools，直连 capability registry）。"""
    status, body, _ = http_get(f"{kernel}/api/v1/tools", timeout=10)
    if status != 200 or not isinstance(body, dict):
        return False
    return any(
        t.get("name") == TOOL_NAME and t.get("plugin_id") == PLUGIN_ID
        for t in body.get("items", [])
    )


def _plugin_in_list(kernel: str, token: str) -> dict | None:
    """GET /api/v1/plugins 里探针条目（None = 不在列表 = 已卸载摘除）。

    该端点在写面鉴权白名单内（GET 也需登录态）。
    """
    status, body, _ = http_get_with_auth(f"{kernel}/api/v1/plugins", token=token, timeout=10)
    if status != 200 or not isinstance(body, list):
        return None
    for item in body:
        if item.get("plugin_id") == PLUGIN_ID:
            return item
    return None


def _echo(kernel: str, message: str):
    """POST /ext 探针端点（auth:none），返回 (status, body)。真 sidecar 执行判据。"""
    return http_post_json(f"{kernel}/ext/{PLUGIN_ID}/echo", {"message": message}, timeout=30)


def _set_enabled(kernel: str, token: str, enabled: bool):
    status, body = _put_json_auth(
        f"{kernel}/api/v1/plugins/{PLUGIN_ID}/enabled", token, {"enabled": enabled}
    )
    assert status == 200, f"PUT enabled={enabled} 期望 200，实际 {status}: {body}"
    assert body.get("success") is True, f"PUT enabled={enabled} 响应: {body}"
    return body


def _wait_until(pred, timeout_s: float, what: str, interval_s: float = 1.0):
    """轮询直到 pred() 为真；超时抛 AssertionError（附 what 描述）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(interval_s)
    raise AssertionError(f"等待超时（{timeout_s}s）: {what}")


# ── 目录操作（Windows 文件锁宽容） ─────────────────────────────────────


def _rename_with_retry(src: Path, dst: Path):
    """目录改名（卸载=摘除/重装=放回）。杀 sidecar 后仍可能遇 AV/句柄延迟，重试。"""
    for attempt in range(20):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.5)


def _nudge_rescan():
    """触发 watcher 重扫。卸载摘除只产 Remove 事件（非相关事件），须主动
    nudge。两发：空目录（Create(Folder)，Linux inotify 可靠）+ rewrite 一份
    plugin.json（Modify(plugin.json) 是明确相关事件——Windows 实测 2026-08-23
    folder-create 偶发丢失，重装段曾等 52s 才靠 60s 轮询兜底）。rewrite
    字节不变 → GAP-6 内容指纹不变，零副作用。"""
    NUDGE_DIR.mkdir(parents=True, exist_ok=True)
    target = PROBE_DIR / "plugin.json"
    if not target.is_file():
        target = SIMPLE_MANIFEST
    data = target.read_bytes()
    target.write_bytes(data)


def _remove_nudge():
    shutil.rmtree(NUDGE_DIR, ignore_errors=True)


# ── 测试 ───────────────────────────────────────────────────────────────


def test_plugin_install_enable_disable_uninstall_lifecycle(kernel_url, auth_token):
    """7.1-7.5 装载生效 → 禁用失效 → 再启用 → 卸载失效 → 重装启用生效。"""
    kernel = kernel_url
    assert PROBE_DIR.is_dir(), (
        f"探针插件目录缺失: {PROBE_DIR}（上次 e2e 中断未恢复？"
        f"请从 stash 恢复: {STASH_DIR} → {PROBE_DIR}"
    )
    profile_bytes = PROFILE_PATH.read_bytes()

    try:
        # ── Phase 0 归零：探针回到 disabled 基线（防上次运行残留） ──
        entry = _plugin_in_list(kernel, auth_token)
        assert entry is not None, (
            "启动期 manifest 未含探针插件（boot 应全量注入含 disabled）——"
            "内核启动时探针目录已存在？"
        )
        if entry.get("enabled") is True or _tool_in_face(kernel):
            _set_enabled(kernel, auth_token, False)
        _wait_until(
            lambda: not _tool_in_face(kernel), 15, "归零：探针工具应不在工具面"
        )

        # ── Phase 7.1 装载生效：启用 → 工具面 + /ext 双活 ──
        _set_enabled(kernel, auth_token, True)
        _wait_until(
            lambda: _tool_in_face(kernel),
            60,
            "7.1 装载生效：PUT enabled=true 后工具应进 /api/v1/tools"
            "（含 G2 复核 spawn sidecar，CI 慢机留足冷启动）",
        )
        status, body, _ = _echo(kernel, "probe-install-live")
        assert status == 200, f"7.1 /ext echo 期望 200，实际 {status}: {body}"
        assert body.get("echo") == "probe-install-live", f"7.1 真 sidecar 回声不符: {body}"
        assert body.get("alive") is True, f"7.1 真 sidecar 存活标记不符: {body}"

        # ── Phase 7.2 禁用失效：能力即时摘除 ──
        _set_enabled(kernel, auth_token, False)
        _wait_until(
            lambda: not _tool_in_face(kernel), 15, "7.2 禁用失效：工具应立即消失"
        )
        status, body, _ = _echo(kernel, "should-404")
        assert status == 404, f"7.2 /ext 期望 404（路由已摘），实际 {status}: {body}"

        # ── Phase 7.3 再启用对称：禁用不丢 manifest，重启用即恢复 ──
        _set_enabled(kernel, auth_token, True)
        _wait_until(lambda: _tool_in_face(kernel), 60, "7.3 再启用：工具应恢复")
        status, body, _ = _echo(kernel, "probe-reenable-live")
        assert status == 200, f"7.3 /ext 期望 200: {status} {body}"
        assert body.get("echo") == "probe-reenable-live", f"7.3 /ext 恢复不符: {body}"

        # ── Phase 7.4 卸载失效：目录摘除 → watcher P1 双面清 + store 无幽灵 ──
        _set_enabled(kernel, auth_token, False)  # 杀缓存 sidecar，解锁 .venv 文件
        _wait_until(lambda: not _tool_in_face(kernel), 15, "7.4 前置：先禁用归零")
        _rename_with_retry(PROBE_DIR, STASH_DIR)
        _nudge_rescan()
        _wait_until(
            lambda: _plugin_in_list(kernel, auth_token) is None,
            75,
            "7.4 卸载失效：插件应从 /api/v1/plugins 摘除（store 无幽灵条目；"
            "上限兜住 watcher 60s 轮询周期）",
        )
        assert not _tool_in_face(kernel), "7.4 卸载后工具不得残留工具面"
        status, _, _ = _echo(kernel, "uninstalled")
        assert status == 404, f"7.4 卸载后 /ext 期望 404，实际 {status}"
        _remove_nudge()

        # ── Phase 7.5 重装启用：目录放回（热发现 disabled 入 store）→ 启用即活 ──
        _rename_with_retry(STASH_DIR, PROBE_DIR)
        _nudge_rescan()

        def _rediscovered_disabled():
            entry = _plugin_in_list(kernel, auth_token)
            return entry is not None and entry.get("enabled") is False

        _wait_until(
            _rediscovered_disabled,
            75,
            "7.5 重装：热发现应把 disabled manifest 送回 store（列表可见且 enabled=false；"
            "上限兜住 watcher 60s 轮询周期）",
        )
        assert not _tool_in_face(kernel), "7.5 重装后未启用，工具不得先出现"
        _set_enabled(kernel, auth_token, True)
        _wait_until(
            lambda: _tool_in_face(kernel),
            60,
            "7.5 重装启用：PUT enabled=true 应真注册（回归锚：修复前 disabled "
            "热发现 manifest 不进 store，此处静默不注册）",
        )
        status, body, _ = _echo(kernel, "probe-reinstall-live")
        assert status == 200, f"7.5 /ext 期望 200: {status} {body}"
        assert body.get("echo") == "probe-reinstall-live", f"7.5 /ext 恢复不符: {body}"
    finally:
        # 数据卫生：目录归位 + nudge 清理 + profile 字节还原（PUT enabled=false
        # 杀 sidecar 防止 .venv 文件锁影响后续车道）。
        _remove_nudge()
        if STASH_DIR.is_dir() and not PROBE_DIR.is_dir():
            try:
                _rename_with_retry(STASH_DIR, PROBE_DIR)
            except PermissionError:
                pass
        if PROBE_DIR.is_dir():
            try:
                _put_json_auth(
                    f"{kernel}/api/v1/plugins/{PLUGIN_ID}/enabled",
                    auth_token,
                    {"enabled": False},
                    timeout=15,
                )
            except Exception:  # noqa: BLE001 —— teardown 尽力而为
                pass
        try:
            PROFILE_PATH.write_bytes(profile_bytes)
        except OSError:
            pass
        shutil.rmtree(STASH_PARENT / "e2e_lifecycle_probe_stash", ignore_errors=True)
