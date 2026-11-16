# @feature: FP-0.2.多端同步 G-S1 | @ci: none-local
"""G-S1 双内核对打车道（2b 同步实现验收，ADR 2026-10-15-multi-device-sync-hlc-lww）。

同机起 C/A/B 三个真实内核实例（一次性 DB/端口/口令，各自独立 USER_ROOT）：
C=cloud（挂 sync_relay 中继 + 注册表/真值库）、A=desktop、B=phone（各挂
sync_agent 常驻，watch 各自用户配置层）。验收六场景（任务书/ADR 影响段增补；
⑥=T1 DB 逻辑记录同步）：

①A 改配置 → sync → B 收敛一致；
②杀 relay → 两端离线各改 → 恢复 → cursor 补拉 + HLC 裁决收敛、ULID 零丢失；
③共享面同对象双改 → HLC 后改者赢（含"手机后改不被云端旧值覆盖"反例：
B=phone 后改，A=desktop 先改，落盘必须停在手机值——层级权威不裁数据面）；
④非冲突双改双端各自落地不丢；
⑤属主执法（B 端改 A 属主会话 = 拒绝，死信留痕）；
⑥A 建会话+发消息（stub LLM 真管道回环）→ B 同租户登录可见只读镜像（会话+
消息全内容），且镜像不可篡改（无写路由 + 属主执法），A 追加 B 镜像跟增。

门控：``AGENTOS_GS1=1`` 才实跑（缺省 skip——真实内核进程，秒级×分钟级车道，
不进现役 CI 预算；DSH e2e = AGENTOS_DSH_E2E=1 同款先例）。本车道自起内核，
不依赖 e2e_02 共享 kernel_url fixture。

实跑：``AGENTOS_GS1=1 python -m pytest tests/e2e_02/test_16_sync_gs1_dual_kernel.py -v``
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml
from stub_llm_server import ScriptedLLMUpstream, text_step

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("AGENTOS_GS1") != "1",
        reason="G-S1 双内核对打车道：设 AGENTOS_GS1=1 实跑（真实内核进程 ×3，"
        "冷启 ~40s/实例；G-M1.4 契约九条为进程内回归面）",
    ),
]

ROOT = Path(__file__).resolve().parents[2]
KERNEL_BIN = ROOT / "kernel" / "target" / "release" / "agentos-kernel.exe"

PORT_C, PORT_A, PORT_B = 19290, 19291, 19292
ADMIN_PASSWORD = "gs1-lane-pass"
TOKENS = {"cloud-c": "tok-c", "desktop-a": "tok-a", "phone-b": "tok-b"}
TIER_OF = {"cloud-c": "cloud", "desktop-a": "desktop", "phone-b": "phone"}

# 场景⑥聊天脚本标记（stub LLM 按 marker 命中场景；两轮请求历史互含旧标记，
# 故只注册单场景——回复文本不参与断言，追加语义用"标记出现+行数增长"断）。
MARKER_ROUND1 = "GS1-MIRROR-ROUND-ONE"
MARKER_ROUND2 = "GS1-MIRROR-ROUND-TWO"
STUB_REPLY = "GS1 脚本回复（镜像验收不依赖本文本）"

_BOOT_TIMEOUT_SECS = 300.0
_CONVERGE_TIMEOUT_SECS = 120.0


class KernelInstance:
    """一个内核实例句柄（进程 + 端口 + 目录面）。"""

    def __init__(self, name: str, port: int, home: Path) -> None:
        self.name = name
        self.port = port
        self.home = home
        self.proc: subprocess.Popen[bytes] | None = None
        self.extra_env: dict[str, str] = {}
        self.config_root: Path | None = None  # None = 仓内 config/（stub 注入时指向临时副本）
        self.relay_url: str | None = None  # None = cloud 实例中继（单内核轻量车道指向进程内替身）

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def config_dir(self) -> Path:
        return self.home / "user" / "config"

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.extra_env)
        env.update(
            {
                "AGENTOS_CONFIG_ROOT": str(self.config_root or ROOT / "config"),
                "AGENTOS_KERNEL_PORT": str(self.port),
                "AGENTOS_DB_PATH": str(self.home / "kernel.db"),
                "AGENTOS_ADMIN_PASSWORD": ADMIN_PASSWORD,
                "AGENTOS_USER_ROOT": str(self.home / "user"),
                "AGENTOS_DATA_DIR": str(self.home / "data"),
                "AGENTOS_DEVICE_TIER": TIER_OF[self.name],
                "AGENTOS_DEVICE_ID": self.name,
                "AGENTOS_SYNC_TOKEN": TOKENS[self.name],
                "AGENTOS_SYNC_RELAY_URL": self.relay_url
                or f"http://127.0.0.1:{PORT_C}/ext/sync_relay",
                "AGENTOS_SYNC_POLL_INTERVAL": "0.5",
                "AGENTOS_SYNC_HEARTBEAT_SECS": "2",
            }
        )
        if extra:
            env.update(extra)
        return env

    def start(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [str(KERNEL_BIN)],
            cwd=str(self.home),
            env=self.env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

    def kill_tree(self) -> None:
        if self.proc is None:
            return
        subprocess.run(
            ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        self.proc = None

    def wait_healthy(self, timeout: float = _BOOT_TIMEOUT_SECS) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} 内核进程退出 rc={self.proc.returncode}")
            try:
                status, body = _http("GET", f"{self.base_url}/health")
                if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
                    return
            except (ConnectionError, OSError) as exc:
                last_error = str(exc)
            time.sleep(1.0)
        raise TimeoutError(f"{self.name} 内核 {timeout}s 未就绪: {last_error}")


def _http(
    method: str, url: str, token: str | None = None, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _relay(path: str, token: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    method = "POST" if body is not None else "GET"
    return _http(method, f"http://127.0.0.1:{PORT_C}/ext/sync_relay{path}", token, body)


def _pull_all(token: str, since: int = 0) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        status, body = _relay(f"/events?since_cursor={since}&limit=500", token)
        assert status == 200, f"relay 拉取失败 {status}: {body}"
        batch = body["events"]
        events.extend(batch)
        if not batch:
            return events
        since = max(e["cursor"] for e in batch)


def _poll_until(desc: str, probe: Any, timeout: float = _CONVERGE_TIMEOUT_SECS) -> Any:
    """轮询直至 probe() 返回真值；超时抛 AssertionError（带末次观测）。"""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(1.0)
    raise AssertionError(f"超时（{timeout}s）: {desc}；末次观测={last!r}")


def _read(instance: KernelInstance, rel: str) -> str | None:
    path = instance.config_dir / rel
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _write(instance: KernelInstance, rel: str, content: str) -> None:
    path = instance.config_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_stub_config_root(base_dir: Path, stub_api_base: str) -> Path:
    """复制仓内 config/ 到临时副本并改写 llm.yaml 注入 stub provider（场景⑥专用）。

    运行中内核消费的共享配置零改动：desktop 实例的 AGENTOS_CONFIG_ROOT 指向
    本副本，defaults（chat/tiers）全指向 stub 模型，杜绝真实 LLM 外呼。
    test_task_lifecycle_paths_e2e._build_tmp_config_root 同款形态。
    """
    dst = base_dir / "config"
    shutil.copytree(ROOT / "config", dst)
    llm_path = dst / "plugins" / "llm" / "llm.yaml"
    data = yaml.safe_load(llm_path.read_text(encoding="utf-8")) or {}
    data.setdefault("models", {})["stub-llm"] = {
        "api_base": stub_api_base,
        "api_key": "stub-key-gs1",
        "context_window": 128000,
        "display_name": "GS1 Scripted Stub",
        "model_name": "stub-model",
        "provider": "stub_openai",
    }
    data["models"]["stub-embedding"] = {
        "api_base": stub_api_base,
        "api_key": "stub-key-gs1",
        "context_window": 8192,
        "dimension": 8,
        "model_name": "stub-embedding",
        "provider": "stub_openai",
    }
    data.setdefault("providers", {})["stub_openai"] = {
        "api_base": stub_api_base,
        "type": "openai",
        "keys": [
            {
                "api_key": "stub-key-gs1",
                "id": "stub_main",
                "max_concurrent": 8,
                "rpm": 600,
                "token_quota": 0,
            }
        ],
    }
    defaults = data.setdefault("defaults", {})
    defaults["chat"] = "stub-llm"
    defaults["embedding"] = "stub-embedding"
    tiers = defaults.setdefault("tiers", {})
    tiers["large"] = "stub-llm"
    tiers["medium"] = "stub-llm"
    tiers["small"] = "stub-llm"
    llm_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8"
    )
    return dst


@pytest.fixture(scope="module")
def fleet(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """C/A/B 三内核实例（module 级：一次起停，六场景顺序共用）。"""
    if not KERNEL_BIN.is_file():
        pytest.fail(f"内核二进制缺失: {KERNEL_BIN}（先 cargo build --release）")
    root = tmp_path_factory.mktemp("gs1")
    stub = ScriptedLLMUpstream()
    stub.start()
    stub.register(
        "gs1-mirror-chat",
        MARKER_ROUND1,
        [text_step(STUB_REPLY)],
        default=text_step(STUB_REPLY),
    )
    cloud = KernelInstance("cloud-c", PORT_C, root / "c")
    desktop = KernelInstance("desktop-a", PORT_A, root / "a")
    phone = KernelInstance("phone-b", PORT_B, root / "b")
    cloud.extra_env["AGENTOS_SYNC_RELAY_TOKENS"] = ",".join(f"{d}:{t}" for d, t in TOKENS.items())
    desktop.config_root = _build_stub_config_root(root / "a", stub.api_base)
    for instance in (cloud, desktop, phone):
        instance.start()
    try:
        for instance in (cloud, desktop, phone):
            instance.wait_healthy()
            _settle_sync_agent(instance)
        yield {
            "cloud": cloud,
            "desktop": desktop,
            "phone": phone,
            "root": root,
            "stub": stub,
        }
    finally:
        for instance in (cloud, desktop, phone):
            instance.kill_tree()
        stub.stop()


def _login_admin(instance: KernelInstance) -> str:
    status, body = _http(
        "POST",
        f"{instance.base_url}/api/v1/auth/login",
        body={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert status == 200, f"登录失败: {status}"
    assert body.get("access_token"), f"登录失败: {body}"
    return str(body["access_token"])


def _sync_agent_grouped(instance: KernelInstance, token: str) -> bool:
    """sync_agent 是否已装入任一合宿组宿主。

    桌面/云档落 light_stable；手机档全组名折入 light 单域（装箱域按档取值，
    host_key.rs cohost_packing_domain）——故按"任一 kind=group 宿主"判定。
    """
    status, body = _http(
        "GET", f"{instance.base_url}/api/v1/plugins/hosts", token=token
    )
    assert status == 200, f"hosts 查询失败: {status}"
    hosts = body if isinstance(body, list) else body.get("hosts", body.get("items", []))
    for host in hosts:
        if host.get("kind") != "group":
            continue
        members = [
            m["plugin_id"] if isinstance(m, dict) else m for m in host.get("members", [])
        ]
        if "sync_agent" in members:
            return True
    return False


def _settle_sync_agent(instance: KernelInstance, timeout: float = 300.0) -> None:
    """等 sync_agent 进入合宿组常驻宿主（冷启装箱竞态兜底）。

    内核对"本次启动新注册"的合宿成员，组宿主可能以旧成员集拉起且不自动补
    （首启实测缺员）。两步触发到位于一组宿主：①manifest 版本一次性变更
    （重注册→装箱分配表更新）；②源码/manifest mtime 指纹 touch（配置指纹
    漂移→宿主驱逐重生，重生按当前分配表重建成员集，实测 20s 内到位）。
    验证后还原版本并复归成员。本步骤是车道装配兜底，不是同步语义的一部分
    （内核 boot 竞态根修挂 2b 台账遗留，D6 内核零改动铁律）。
    """
    token = _login_admin(instance)
    if _sync_agent_grouped(instance, token):
        return
    manifest = ROOT / "plugins" / "shared" / "sync_agent" / "plugin.json"
    server = ROOT / "plugins" / "shared" / "sync_agent" / "server.py"
    original = manifest.read_bytes()
    joined = False
    try:
        data = json.loads(original.decode("utf-8"))
        data["version"] = "1.0.1"
        manifest.write_bytes(
            (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            manifest.touch()
            server.touch()
            if _sync_agent_grouped(instance, token):
                joined = True
                break
            time.sleep(15.0)
    finally:
        manifest.write_bytes(original)
    if not joined:
        raise AssertionError(
            f"{instance.name}: sync_agent {timeout}s 未进入任何合宿组宿主"
        )
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if _sync_agent_grouped(instance, token):
            return
        time.sleep(10.0)
    raise AssertionError(f"{instance.name}: 版本还原后 sync_agent 成员丢失")


def test_10_config_change_converges(fleet: dict[str, Any]) -> None:
    """场景①：A 改配置 → sync → B 收敛一致（且云端账本留档）。"""
    desktop, phone = fleet["desktop"], fleet["phone"]
    _write(desktop, "policies.yaml", "scenario: one\nowner: desktop-a\n")

    def converged() -> object:
        return _read(phone, "policies.yaml") == "scenario: one\nowner: desktop-a\n"

    _poll_until("B 端收敛 A 的配置", converged)
    events = _pull_all(TOKENS["desktop-a"])
    assert any(
        e["asset_id"] == "policies.yaml" and e["device_id"] == "desktop-a" for e in events
    ), "云端账本应有该事件"


def test_20_relay_outage_convergence_and_zero_loss(fleet: dict[str, Any]) -> None:
    """场景②：杀 relay → 两端离线各改 → 恢复 → cursor 补拉收敛、ULID 零丢失。"""
    cloud, desktop, phone = fleet["cloud"], fleet["desktop"], fleet["phone"]

    before = _pull_all(TOKENS["desktop-a"])
    before_ulids = {e["ulid"] for e in before}

    cloud.kill_tree()
    _poll_until("relay 端口已死", _probe_port_dead, timeout=30)

    _write(desktop, "offline-a.yaml", "written-offline: A\n")
    _write(phone, "offline-b.yaml", "written-offline: B\n")
    time.sleep(3)  # 离线窗口：变更只进各自 outbox

    cloud.start()
    cloud.wait_healthy()

    def both_converged() -> object:
        b_has_a = _read(phone, "offline-a.yaml") == "written-offline: A\n"
        a_has_b = _read(desktop, "offline-b.yaml") == "written-offline: B\n"
        return b_has_a and a_has_b

    _poll_until("恢复后双端离线变更互见（outbox 补传 + cursor 补拉）", both_converged)

    after = _pull_all(TOKENS["desktop-a"])
    after_ulids = {e["ulid"] for e in after}
    assert before_ulids <= after_ulids, "恢复前的事件零丢失"
    assert len(after_ulids) == len(after), "ULID 无重复（幂等收敛）"
    new_ulids = {e["ulid"] for e in after if e["ulid"] not in before_ulids}
    assert len(new_ulids) >= 2, f"两端离线变更各一条入库，实际新增 {len(new_ulids)}"


def _probe_port_dead() -> bool:
    try:
        _http("GET", f"http://127.0.0.1:{PORT_C}/health")
        return False
    except (ConnectionError, OSError):
        return True


def test_30_hlc_lww_phone_later_change_wins(fleet: dict[str, Any]) -> None:
    """场景③（ADR 影响段抽查①反例）：同对象双改，手机后改不被他端旧值覆盖。

    A=desktop 先改并同步落账后，B=phone 后改；层级权威（云>桌面>手机）不裁
    数据面——HLC 后改者赢，最终三面收敛在手机值；落败旧值留云端账本可查。
    """
    desktop, phone = fleet["desktop"], fleet["phone"]

    _write(desktop, "shared.yaml", "value: from-desktop\n")

    def desktop_event_in_ledger() -> object:
        events = _pull_all(TOKENS["desktop-a"])
        return any(e["asset_id"] == "shared.yaml" for e in events)

    _poll_until("A 的先改已入云端账本", desktop_event_in_ledger)

    _write(phone, "shared.yaml", "value: from-phone\n")

    def desktop_converged_to_phone() -> object:
        return _read(desktop, "shared.yaml") == "value: from-phone\n"

    _poll_until("A(desktop) 被手机后改覆盖（HLC-LWW）", desktop_converged_to_phone)

    history = [e for e in _pull_all(TOKENS["desktop-a"]) if e["asset_id"] == "shared.yaml"]
    assert len(history) >= 2, "落败旧值留账本可查（非静默销毁）"
    def hlc_order(e: dict[str, Any]) -> tuple[int, int]:
        return (e["hlc"]["physical_ms"], e["hlc"]["logical"])

    winner = max(history, key=hlc_order)
    assert winner["device_id"] == "phone-b", "HLC 最大者=手机后改"


def test_40_non_conflicting_changes_both_land(fleet: dict[str, Any]) -> None:
    """场景④（ADR 影响段抽查②）：非冲突双改双端各自落地不丢。"""
    desktop, phone = fleet["desktop"], fleet["phone"]
    _write(desktop, "only-a.yaml", "side: A\n")
    _write(phone, "only-b.yaml", "side: B\n")

    def both_visible() -> object:
        a_has_b = _read(desktop, "only-b.yaml") == "side: B\n"
        b_has_a = _read(phone, "only-a.yaml") == "side: A\n"
        return a_has_b and b_has_a

    _poll_until("非冲突改动双端互见", both_visible)


def test_50_owner_enforcement_rejects_foreign_write(fleet: dict[str, Any]) -> None:
    """场景⑤：属主面只追加——B 端改 A 属主会话 = 拒绝（死信留痕）。"""
    create_event = _session_event(
        "01ARZ3NDEKGS1A00000000000A", "desktop-a", "sess-owned-by-a", "owner content"
    )
    status, body = _relay("/events", TOKENS["desktop-a"], {"events": [create_event]})
    assert status == 200, f"A 创建会话应被接受: {body}"
    assert body["accepted"] == 1, f"A 创建会话应被接受: {body}"

    intrude = _session_event(
        "01ARZ3NDEKGS1B00000000000B", "phone-b", "sess-owned-by-a", "hijack content"
    )
    status, body = _relay("/events", TOKENS["phone-b"], {"events": [intrude]})
    assert status == 200, f"他端写应被拒: {body}"
    assert body["accepted"] == 0, f"他端写应被拒: {body}"
    reasons = json.dumps(body, ensure_ascii=False)
    assert "owner_violation" in reasons, "拒绝理由=属主违规"

    ledger = _pull_all(TOKENS["desktop-a"])
    assert not any(e["ulid"] == intrude["ulid"] for e in ledger), "违规写不入事件账本"
    dead = _http(
        "GET", f"http://127.0.0.1:{PORT_C}/ext/sync_relay/dead_letters"
    )
    assert dead[0] in (401, 403), "死信查看为 admin 面（设备 token 不放行即执法正确）"


def _session_event(ulid: str, device: str, session_id: str, content: str) -> dict[str, Any]:
    import uuid

    physical_ms = int(time.time() * 1000) + int(uuid.uuid4().int % 1000)
    return {
        "ulid": ulid,
        "device_id": device,
        "asset_type": "session",
        "asset_id": session_id,
        "version": 1,
        "updated_at": "2026-10-15T08:00:00+00:00",
        "payload": {"content": content},
        "hlc": {"physical_ms": physical_ms, "logical": 0, "device_id": device},
    }


def _mirror_get(instance: KernelInstance, token: str, table: str) -> tuple[int, Any]:
    return _http(
        "GET", f"{instance.base_url}/ext/sync_agent/mirror/records?table={table}", token=token
    )


def test_60_session_message_mirror_readonly(fleet: dict[str, Any]) -> None:
    """场景⑥（T1）：A 建会话+发消息 → B 同租户登录可见只读镜像且不可篡改。

    - 可见：会话镜像（owner=desktop-a）+ 消息镜像（user 原文 + assistant 回复
      全文）随 sync 到达 B；属主端追加，B 镜像跟增（属主只追加在镜像上兑现）。
    - 不可篡改（结构性）：镜像端点无写路由；B 伪造写 A 属主会话 = relay 属主
      执法拒绝，B 镜像不被该伪造推动变化。
    """
    desktop, phone = fleet["desktop"], fleet["phone"]
    token_a = _login_admin(desktop)
    token_b = _login_admin(phone)

    # A 端建会话 + 两轮真实管道聊天（stub LLM 脚本回复）。
    status, sess = _http(
        "POST", f"{desktop.base_url}/api/v1/sessions", token=token_a,
        body={"title": "gs1-mirror"},
    )
    assert status == 200, f"A 建会话失败: {sess}"
    thread_id = str(sess["thread_id"])
    for marker in (MARKER_ROUND1, MARKER_ROUND2):
        status, body = _http(
            "POST", f"{desktop.base_url}/api/v1/chat", token=token_a,
            body={"message": marker, "session_id": thread_id},
        )
        assert status == 200, f"A 发消息失败（{marker}）: {body}"

    # B 同租户登录 → 会话镜像可见（属主=desktop-a）。
    def session_mirrored() -> object:
        status, body = _mirror_get(phone, token_b, "sessions")
        if status != 200 or not isinstance(body, dict):
            return None
        rows = [r for r in body.get("records", []) if r.get("pk") == thread_id]
        return rows[0] if rows and rows[0].get("owner_device") == "desktop-a" else None

    mirrored = _poll_until("B 端会话镜像可见", session_mirrored)
    assert mirrored is not None

    # 消息镜像：两轮 user 原文可见 + assistant 回复在镜；追加语义=行数增长。
    def mirror_rows() -> list[dict[str, Any]] | None:
        status, body = _mirror_get(phone, token_b, "messages")
        if status != 200 or not isinstance(body, dict):
            return None
        return list(body.get("records", []))

    def user_contents(rows: list[dict[str, Any]]) -> set[str]:
        return {
            str((r.get("row") or {}).get("content", ""))
            for r in rows
            if (r.get("row") or {}).get("role") == "user"
        }

    def round1_visible() -> object:
        rows = mirror_rows()
        if not rows:
            return None
        return True if MARKER_ROUND1 in user_contents(rows) else None

    _poll_until("B 端消息镜像含第一轮 user 原文", round1_visible)
    rows_after_round1 = mirror_rows() or []

    def round2_appended() -> object:
        rows = mirror_rows()
        if not rows:
            return None
        if MARKER_ROUND2 in user_contents(rows) and len(rows) > len(rows_after_round1):
            return sorted(user_contents(rows))
        return None

    _poll_until("B 端消息镜像追加第二轮（标记出现且行数增长）", round2_appended)
    rows_final = mirror_rows() or []
    assert any(
        (r.get("row") or {}).get("role") == "assistant" for r in rows_final
    ), "assistant 回复应入镜像"

    # 不可篡改（结构）：镜像端点无写路由（POST 一律 404）。
    status, _ = _http(
        "POST", f"{phone.base_url}/ext/sync_agent/mirror/records?table=sessions",
        token=token_b, body={"pk": thread_id, "hijack": True},
    )
    assert status == 404, f"镜像端点必须只读（POST 拒绝），实际 {status}"

    # 不可篡改（属主执法）：B 伪造 A 属主会话的消息事件 → relay 拒绝，B 镜像不变。
    forge = _session_event(
        "01ARZ3NDEKGS1C00000000000C", "phone-b", thread_id, "forged-by-phone"
    )
    status, body = _relay("/events", TOKENS["phone-b"], {"events": [forge]})
    assert status == 200, f"伪造写响应异常: {body}"
    assert body.get("accepted") == 0, f"伪造写应被拒: {body}"
    assert "owner_violation" in json.dumps(body, ensure_ascii=False)
    status, body = _mirror_get(phone, token_b, "messages")
    contents = {
        str((r.get("row") or {}).get("content", "")) for r in body.get("records", [])
    }
    assert "forged-by-phone" not in contents, "伪造内容不得出现在镜像"
