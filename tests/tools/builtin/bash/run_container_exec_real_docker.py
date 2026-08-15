"""真实 docker 容器端到端验证（手动运行，不进 CI）。

验证容器路径的招牌行为在真实 docker 下成立：
1. start_process + container_id 用 docker exec 起进程
2. execute 超时 → 返回 status=running + pid（容器内 pid）
3. continue → 进程完成后返回 status=completed + exit_code
4. terminate → 容器内进程被 kill（单进程，不残留）

运行方式（仓库根目录）：
    python tests/tools/builtin/bash/test_container_exec_real_docker.py

前提：本机 docker daemon 可用，有 python:3.11-slim 镜像。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

# 0.2 架构：bash 工具位于 plugins/shared/tools/bash（平铺 import）。
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "plugins" / "shared" / "tools" / "bash"))

from bash_types import WorkUnit  # noqa: E402
from process_manager import (  # noqa: E402
    ContainerProcessBackend,
    ProcessManager,
)

CONTAINER_NAME = "agentos_test_container_exec"
IMAGE = "python:3.11-slim"


def run_docker(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """同步跑 docker 命令。"""
    return subprocess.run(
        ["docker"] + args,
        capture_output=True,
        text=True,
        check=check,
    )


def setup_container() -> None:
    """起一个常驻容器（docker run -d + sleep infinity），供测试 exec。"""
    # 先清理同名旧容器
    run_docker(["rm", "-f", CONTAINER_NAME], check=False)
    print(f"[setup] 启动容器 {CONTAINER_NAME} (image={IMAGE})...")
    # -d 后台跑，sleep infinity 保持常驻；--init 避免 PID1 僵尸问题
    result = run_docker(
        ["run", "-d", "--name", CONTAINER_NAME, "--init", IMAGE, "sleep", "infinity"]
    )
    cid = result.stdout.strip()
    print(f"[setup] 容器已启动 cid={cid[:12]}")


def teardown_container() -> None:
    print(f"[teardown] 删除容器 {CONTAINER_NAME}...")
    run_docker(["rm", "-f", CONTAINER_NAME], check=False)
    print("[teardown] 完成")


async def test_execute_running_continue_completed(pm: ProcessManager) -> None:
    """完整链路：execute(超时→running) → continue(完成→completed)。"""
    print("\n=== 测试 1: execute→running→continue→completed ===")

    # sleep 3 秒后输出 done。用 sh -c 显式包复合命令（exec 作用于整体）。
    pid, _ = await pm.start_process(
        command="sh -c 'sleep 3; echo done_marker_42'",
        working_dir="/tmp",
        container_id=CONTAINER_NAME,
    )
    print(f"[1] start_process 返回 pid={pid}")

    info = pm.get_process_info(pid)
    assert info is not None, "进程信息应存在"
    container_pid = info.metadata.get("container_pid")
    print(f"[1] 容器内 pid={container_pid}（host pid={pid}）")
    assert container_pid is not None and container_pid > 1, (
        f"容器内 pid 应是 >1 的整数，实际: {container_pid}"
    )
    assert container_pid != pid, "容器内 pid 不应等于 host pid（不同 namespace）"
    assert isinstance(info.backend, ContainerProcessBackend), "backend 应是 ContainerProcessBackend"

    # 等 1.5 秒让进程真的在跑（还没到 sleep 3 完成）
    await asyncio.sleep(1.5)
    info = pm.get_process_info(pid)
    assert info is not None and info.status == "running", (
        f"1.5s 时进程应还在 running，实际 status={info.status if info else None}"
    )
    print(f"[1] 1.5s 时 status={info.status} ✓（还在跑，符合预期）")

    # 等进程自然完成（sleep 3，总共等约 4s）
    print("[1] 等待容器内 sleep 3 完成...")
    for _ in range(40):  # 最多等 8 秒
        await asyncio.sleep(0.2)
        info = pm.get_process_info(pid)
        if info and info.status != "running":
            break
        info = pm.get_process_info(pid)

    assert info is not None, "进程信息应仍存在"
    print(f"[1] 进程结束 status={info.status} exit_code={info.exit_code}")
    assert info.status == "completed", f"应 completed，实际 {info.status}"
    assert info.exit_code == 0, f"应 exit_code=0，实际 {info.exit_code}"

    # 读日志，验证 echo done_marker_42 真的执行了
    output = pm.get_output(pid)
    print(f"[1] 日志输出片段: ...{output[-100:]!r}")
    assert "done_marker_42" in output, "容器内 echo 输出应出现在日志里"
    print("[1] ✓ execute→running→completed 完整链路验证通过")


async def test_terminate_kills_container_process(pm: ProcessManager) -> None:
    """terminate → 容器内进程被 kill，不残留。"""
    print("\n=== 测试 2: terminate 杀容器内进程 ===")

    # 起一个 sleep 300（长进程）
    pid, _ = await pm.start_process(
        command="sleep 300",
        working_dir="/tmp",
        container_id=CONTAINER_NAME,
    )
    info = pm.get_process_info(pid)
    assert info is not None
    container_pid = info.metadata["container_pid"]
    print(f"[2] 起 sleep 300，容器内 pid={container_pid}")

    # 确认容器内确实有这个进程
    ps_before = run_docker(
        ["exec", CONTAINER_NAME, "sh", "-c", f"kill -0 {container_pid} 2>/dev/null && echo ALIVE || echo DEAD"]
    ).stdout.strip()
    print(f"[2] terminate 前容器内进程状态: {ps_before}")
    assert ps_before == "ALIVE", f"terminate 前进程应 ALIVE，实际 {ps_before}"

    # terminate
    ok, err = await pm.terminate_process(pid, force=True)
    print(f"[2] terminate 结果: ok={ok} err={err}")
    assert ok, f"terminate 应成功，err={err}"

    # 验证容器内进程已被杀（单进程 kill）
    await asyncio.sleep(0.3)  # 给 kill 一点时间生效
    ps_after = run_docker(
        ["exec", CONTAINER_NAME, "sh", "-c", f"kill -0 {container_pid} 2>/dev/null && echo ALIVE || echo DEAD"]
    ).stdout.strip()
    print(f"[2] terminate 后容器内进程状态: {ps_after}")
    assert ps_after == "DEAD", f"terminate 后进程应 DEAD，实际 {ps_after}（容器内残留！）"
    print("[2] ✓ terminate 在容器内单进程杀生效，无残留")


async def test_execute_timeout_does_not_kill(pm: ProcessManager) -> None:
    """execute 超时返回后（用 start_process 直接验证），容器内进程仍存活。

    注：这里直接在 ProcessManager 层验证——start_process 不杀进程，
    超时是 BashTool 层的轮询行为。我们验证 ProcessManager 启动后进程持续存活。
    """
    print("\n=== 测试 3: 进程启动后持续存活（不被 ProcessManager 误杀）===")

    pid, _ = await pm.start_process(
        command="sleep 60",
        working_dir="/tmp",
        container_id=CONTAINER_NAME,
    )
    info = pm.get_process_info(pid)
    assert info is not None
    container_pid = info.metadata["container_pid"]
    print(f"[3] 起 sleep 60，容器内 pid={container_pid}")

    # 等 2 秒，进程应仍存活（没有被 ProcessManager 误杀）
    await asyncio.sleep(2)
    ps = run_docker(
        ["exec", CONTAINER_NAME, "sh", "-c", f"kill -0 {container_pid} 2>/dev/null && echo ALIVE || echo DEAD"]
    ).stdout.strip()
    print(f"[3] 启动 2s 后容器内进程状态: {ps}")
    assert ps == "ALIVE", f"启动后进程应持续 ALIVE，实际 {ps}"

    # 清理：terminate 掉
    await pm.terminate_process(pid, force=True)
    print("[3] ✓ 进程启动后不被误杀，符合'超时不杀进程'语义")


async def main() -> int:
    try:
        setup_container()
        # 用临时日志目录
        log_dir = Path("./logs/test_container_exec_real")
        log_dir.mkdir(parents=True, exist_ok=True)
        pm = ProcessManager(log_dir=log_dir)

        await test_execute_running_continue_completed(pm)
        await test_terminate_kills_container_process(pm)
        await test_execute_timeout_does_not_kill(pm)

        print("\n" + "=" * 60)
        print("✅ 所有真实 docker 验证通过")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print(f"\n❌ 验证失败: {e}")
        return 1
    except Exception:
        import traceback
        traceback.print_exc()
        return 2
    finally:
        teardown_container()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
