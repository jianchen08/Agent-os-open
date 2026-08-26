# @feature: FP-0.2.插件合宿进程模型 | @ci: python-e2e
"""E2E：合宿（co-hosting）进程监控——验证 light 插件共享宿主进程而非每插件一进程。

真实内核（:9100）+ 真实 sidecar + 真实 LLM chat 全链路，psutil 进程级断言：

  1. chat 全链路跑通（7 个 light guard 成员在管道链中被真实调用）；
  2. 进程形态断言：light 成员只以 host.py 合宿进程形态存在——
     - 合宿宿主进程数 = ceil(7 / 挂载上限 6) = 2（同一组内共享一个进程）；
     - 7 个 light 成员全部出现在宿主 --members 注入列表（装箱覆盖全）；
     - 没有任何 light 成员以"独占 sidecar"形态存在（判别：exe =
       plugins/shared/pipeline/*/<name>/.venv 的 python，即每插件独立进程）。

运行前提：
- 内核已启动（AGENTOS_DB_PATH=":memory:" AGENTOS_KERNEL_PORT=9100
  ./kernel/target/release/agentos-kernel.exe），9100 端口可访问。
- 依赖真实 LLM（灵汐回复）：无 ZHIPU_API_KEY 时跳过（与 test_05 同守卫）。
- 共享合宿 venv 已建（plugins/shared/_host/.venv；CI e2e.yml 按 _host/uv.lock
  uv sync 构建）。

手动运行：python -m pytest tests/e2e_02/test_10_cohost_host_grouping.py -q
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import psutil
import pytest
from e2e_helpers import KERNEL_URL, create_session, http_post_json_auth

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.timeout(420),
    pytest.mark.skipif(
        not os.environ.get("ZHIPU_API_KEY"),
        reason="需要 ZHIPU_API_KEY（真实 LLM 回复）",
    ),
]

CHAT_PROMPT = "请只回复两个字：好的"

# 已声明 host_group: light 的 7 个插件（前 4 个 + 后 3 个为扩展批）
LIGHT_PLUGIN_IDS = [
    "pipeline_pause_guard",
    "pipeline_level_guard",
    "pipeline_stop_check",
    "pipeline_duplicate_check",
    "pipeline_stuck_detector",
    "pipeline_task_reminder",
    "pipeline_termination_advisor",
]
# 挂载上限默认 6（AGENTOS_LIGHT_HOST_MAX_MEMBERS 可覆盖）→ 7 成员恰 2 宿主
EXPECTED_HOST_COUNT = 2
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _light_member_exes() -> list[str]:
    """7 个 light 成员各自的独立 sidecar venv python 可执行路径。

    合宿生效判据：这些 exe 一个都不该作为独立进程存在（成员应只出现在
    共享宿主 _host/.venv 的 host.py 进程里，见 _host_venv_python）。
    """
    exes: list[str] = []
    for pid in LIGHT_PLUGIN_IDS:
        # 插件目录：plugins/shared/pipeline/{input,output}/<name>
        for phase in ("input", "output"):
            exe = (
                _PROJECT_ROOT
                / "plugins"
                / "shared"
                / "pipeline"
                / phase
                / pid.removeprefix("pipeline_")
                / ".venv"
                / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
            if exe.is_file():
                exes.append(str(exe))
                break
    return exes


def _host_venv_python() -> str:
    """合宿宿主共享 venv 的 python 可执行路径。"""
    return str(
        _PROJECT_ROOT
        / "plugins"
        / "shared"
        / "_host"
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )


def _venv_base_python() -> str:
    """共享 venv 的 base 解释器（Windows launcher 会再 spawn 的 uv base python）。

    venv 的 pyvenv.cfg 的 home 字段指向 base 安装目录，base python 与其同目录
    （Windows：home/python.exe；POSIX：home/bin/python）。读不到时返回空串
    （断言按"共享 venv python 或其 base"匹配，兜底只认 venv 自身）。
    """
    venv_dir = _PROJECT_ROOT / "plugins" / "shared" / "_host" / ".venv"
    cfg = venv_dir / "pyvenv.cfg"
    if not cfg.is_file():
        return ""
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("home"):
            home = line.split("=", 1)[1].strip()
            return os.path.join(home, "python.exe" if os.name == "nt" else "python")
    return ""


def _snapshot_host_processes() -> list:
    """采集当前 host.py 合宿宿主组（按 (slot, members) 去重）。

    Windows venv 双进程结构：`.venv\\Scripts\\python.exe` 是 launcher，会再
    spawn 真身（uv base python）——同一宿主的 host.py 命令出现两个 OS 进程
    （launcher 的 ppid = 内核，真身 ppid = launcher）。宿主**组**才是逻辑
    进程单位，故按 (slot, members) 去重返回 (代表 pid, members)。
    """
    groups: dict[tuple, list] = {}
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = p.info["cmdline"] or []
            joined = " ".join(cmdline)
            if "host.py" in joined and "--group" in joined and "light" in joined:
                members = []
                slot = None
                for i, arg in enumerate(cmdline):
                    if arg == "--members" and i + 1 < len(cmdline):
                        members = sorted(cmdline[i + 1].split(","))
                    if arg == "--slot" and i + 1 < len(cmdline):
                        slot = cmdline[i + 1]
                groups.setdefault((slot, tuple(members)), []).append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return [(pids, members) for (_slot, members), pids in sorted(groups.items())]


def _stable_hosts(timeout: float = 60.0) -> list:
    """轮询直至宿主进程形态稳定：成员并集 == 全部 light 成员且连续两次快照一致。

    装箱是动态的：每个新成员首次调用都会触发整组 respawn（成员集变化 → 宿主
    指纹变化 → 旧宿主被杀、新宿主载入新成员集）。Windows 上 taskkill 异步，
    旧代宿主可能残留几百 ms——立即快照会撞上过渡窗口（实测出现两代并存），
    必须等稳态：并集覆盖全部成员且连续两次快照相同。
    """
    last: list = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hosts = _snapshot_host_processes()
        union = sorted({m for _, members in hosts for m in members})
        if hosts and union == sorted(LIGHT_PLUGIN_IDS):
            if hosts == last:
                return hosts
            last = hosts
        time.sleep(2)
    return last


def _solo_light_processes() -> list:
    """以"成员各自 venv python 起 server.py"形态存在的独立 sidecar 进程。"""
    solo_exes = set(_light_member_exes())
    found: list = []
    for p in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            if p.info["exe"] in solo_exes:
                found.append((p.pid, p.info["exe"]))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


@pytest.fixture(scope="module")
def chat_flow(auth_token):
    """登录 + 建会话 + 一次真实 LLM chat 调用，触发全管道执行（7 light 成员全部落位）。"""
    token = auth_token
    session = create_session(token, title="e2e-cohost-grouping")
    status, body, _ = http_post_json_auth(
        f"{KERNEL_URL}/api/v1/chat",
        {"message": CHAT_PROMPT, "session_id": session["thread_id"]},
        token=token,
        timeout=150,  # LLM 生成宽松超时
    )
    return {
        "token": token,
        "session": session,
        "chat_status": status,
        "chat_body": body if isinstance(body, dict) else {},
    }


class TestCohostHostGrouping:
    """合宿宿主进程形态：真内核 + 真管道调用 + psutil 进程断言。"""

    @pytest.mark.timeout(120)
    def test_chat_succeeds_and_hosts_spawned(self, chat_flow):
        """chat 200 且 LLM 回复非空；此后宿主进程按预期落位（轮询至多 60s）。"""
        assert chat_flow["chat_status"] == 200, (
            f"/api/v1/chat 期望 200，实际 {chat_flow['chat_status']}"
        )
        content = chat_flow["chat_body"].get("content", "")
        assert isinstance(content, str) and len(content.strip()) > 0, (
            "content 不应为空（期望 LLM 回复）"
        )

        # 管道已跑完 → 7 个 light 成员全部被装箱调用 → 宿主进程应已 spawn。
        # （idle GC 阈值默认 300s，测试窗口内宿主存活；轮询至稳态。）
        hosts = _stable_hosts()
        assert hosts, "未发现 host.py 合宿宿主进程（light 装箱未生效？）"

    @pytest.mark.timeout(60)
    def test_light_members_share_two_hosts_not_solo(self, chat_flow):
        """进程形态：7 成员 = 2 个宿主（每宿主 ≤6），且无任何成员独立进程。

        这正是"合宿 vs 每插件一进程"的可观测差异：若 host_group 未生效，
        7 个成员会以 7 个独立 sidecar（各自 .venv python）进程存在。
        """
        hosts = _stable_hosts()
        assert len(hosts) == EXPECTED_HOST_COUNT, (
            f"7 个 light 成员应装箱到 {EXPECTED_HOST_COUNT} 个宿主进程，"
            f"实际 {len(hosts)} 个：{hosts}"
        )

        # 成员集并集 == 全部 7 个 light 成员（一个不落）
        union = sorted({m for _, members in hosts for m in members})
        assert union == sorted(LIGHT_PLUGIN_IDS), (
            f"宿主 --members 并集应覆盖全部 light 成员，实际 {union}"
        )

        # 无独立 sidecar 进程（判别：exe 为成员各自 .venv 的 python）
        solo = _solo_light_processes()
        assert not solo, (
            f"light 成员不应以独立 sidecar 进程存在（合宿未生效），发现 {solo}"
        )

        # 每宿主进程实际运行共享 venv 的 python（Windows venv = launcher + base
        # 真身双进程，两者都属于共享 venv 底座而非成员各自 venv）
        host_venv = _host_venv_python()
        for group_pids, _members in hosts:
            for pid in group_pids:
                try:
                    exe = psutil.Process(pid).exe()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                assert exe in (host_venv, _venv_base_python()), (
                    f"宿主进程 {pid} 应运行共享 venv python（{host_venv} 或其 base），"
                    f"实际 {exe}"
                )
