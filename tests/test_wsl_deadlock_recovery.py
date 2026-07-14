"""
WSL2 内核 D 状态死锁的自动恢复链路回归测试

变更背景：
  实际启动报错（两次复现）：
    docker.exe : Error response from daemon: Cannot restart container ...
      tried to kill container, but did not receive an exit event
    /wsl_ensure_containers.sh: line 199: out: unbound variable
    [ERROR] container start failed (rc=1)   ← 注意是 rc=1，不是 rc=7

  根因（两处脚本 bug，叠加导致自动 wsl --shutdown 重试链路失效）：

  1) wsl_ensure_containers.sh 第 199 行 `echo "$out"` —— $out 从未被赋值，
     在 `set -uo pipefail` 下触发 unbound variable，导致 `if grep` 条件被
     短路为假，脚本走通用错误分支 exit 1（而非 exit 7）。
     上层 start_web_cn.bat 收到 rc=1 而非 rc=7，于是**不触发**自动
     wsl --shutdown 重试，用户被迫手动干预。

  2) 即便 $out 修好，死锁特征正则只覆盖 "create task 失败"类，不匹配
     实际场景的 "stop/kill 失败"（did not receive an exit event）。

  3) update_frontend.ps1 第 183 行 `docker restart` 在 ErrorActionPreference=Stop
     下，docker.exe 写 stderr 触发 NativeCommandError terminating error，
     中断脚本（第一次启动即如此）。

修复策略：
  - 让 run_with_idle_timeout 把命令输出持久化到 IDLE_LOG_FILE 文件，
    compose up 失败分支 grep 该文件（而非未定义的 $out）
  - 死锁正则追加 stop/kill 失败特征
  - docker restart 临时切 Continue 模式，失败仅 warn 不中断

验证范围：
  1. wsl_ensure_containers.sh 不再引用未定义的 $out
  2. 死锁特征正则覆盖 stop/kill 失败（did not receive an exit event）
  3. compose 输出被持久化且失败分支 grep 该文件
  4. update_frontend.ps1 的 docker restart 容忍 stderr（NativeCommandError）

测试范式：复刻 tests/test_startup_env_adaptation.py 的静态源码字符串断言
（项目无 bats/Pester 工具链，pytest 静态断言是既有约定）。
"""
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_file(rel_path: str) -> str:
    full = PROJECT_ROOT / rel_path
    assert full.exists(), f"文件不存在: {full}"
    return full.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. $out 未定义变量已清除（修复 unbound variable 短路）
# ---------------------------------------------------------------------------
class TestNoUndefinedOutVariable:
    """验证 wsl_ensure_containers.sh 不再引用从未赋值的 $out 变量。

    $out 在修复前仅出现在第 199 行 `echo "$out" | grep`，全文无任何赋值。
    `set -uo pipefail` 下触发 unbound variable，`if` 条件被短路为假，
    死锁识别失效，脚本误走 exit 1（而非 exit 7），上层不触发自动重试。
    """

    def test_no_out_variable_reference(self):
        content = _read_file("wsl_ensure_containers.sh")
        assert '"$out"' not in content, (
            "wsl_ensure_containers.sh 仍引用未定义的 $out 变量，"
            "在 set -uo pipefail 下触发 unbound variable，死锁识别失效"
        )


# ---------------------------------------------------------------------------
# 2. 死锁特征正则覆盖 stop/kill 失败场景
# ---------------------------------------------------------------------------
class TestDeadlockPatternCoversStopKill:
    """验证死锁特征正则覆盖 stop/kill 失败，而非仅 create task 失败。

    实际报错："tried to kill container, but did not receive an exit event"
    属 stop/kill 失败（WSL2 内核 D 状态死锁导致 kill 信号无响应）。
    修复前正则只匹配 create task 失败，漏判此类死锁 → 走 exit 1 而非 exit 7。
    """

    def test_pattern_covers_exit_event_timeout(self):
        content = _read_file("wsl_ensure_containers.sh")
        assert "did not receive an exit event" in content, (
            "死锁特征正则未覆盖 stop/kill 失败（did not receive an exit event），"
            "实际场景会被漏判，无法触发自动 wsl --shutdown 重试"
        )


# ---------------------------------------------------------------------------
# 3. compose 输出被持久化，失败分支 grep 文件而非未定义变量
# ---------------------------------------------------------------------------
class TestComposeOutputCaptured:
    """验证 compose up 的输出被持久化到文件，供失败分支 grep 分析。

    修复前 COMPOSE_OUT 仅被定义（=赋值）和 rm，从未被读取用于 grep，
    是作者预留但未接上的半成品。run_with_idle_timeout 把输出打到 stdout
    但不存变量，导致失败分支无文本可分析。
    """

    def test_compose_out_referenced_in_grep(self):
        """COMPOSE_OUT 必须在 grep 命令中作为输入文件被读取（而非仅定义+rm）。

        精确判断：存在一行同时含 grep 和 $COMPOSE_OUT。
        修复前 COMPOSE_OUT 仅出现在赋值行和 rm 行，grep 行用的是未定义的 $out，
        故 grep 与 $COMPOSE_OUT 不会出现在同一行。
        """
        content = _read_file("wsl_ensure_containers.sh")
        assert 'COMPOSE_OUT=' in content, "应定义 COMPOSE_OUT 变量"
        assert 'grep' in content, "应有 grep 死锁特征分析"
        # 精确断言：存在某一行同时含 grep 和 $COMPOSE_OUT（即 grep 读取该文件）
        has_grep_reading_compose_out = any(
            'grep' in line and 'COMPOSE_OUT' in line
            for line in content.splitlines()
        )
        assert has_grep_reading_compose_out, (
            "COMPOSE_OUT 应在 grep 命令中被读取（grep 与 $COMPOSE_OUT 同行），"
            "而非仅出现在赋值和 rm 两处——否则失败分支无文本可分析，"
            "只能退回引用未定义的 $out 触发 unbound variable"
        )


# ---------------------------------------------------------------------------
# 4. update_frontend.ps1 的 docker restart 容忍 stderr
# ---------------------------------------------------------------------------
class TestDockerRestartToleratesStderr:
    """验证 docker restart 在 ErrorActionPreference=Stop 下不中断脚本。

    docker.exe 把诊断信息写到 stderr（即使成功时也写），PowerShell 在
    ErrorActionPreference=Stop 下会把 native command 的 stderr 当作
    terminating error 抛出 NativeCommandError，中断脚本执行。
    修复：restart 前后切换 ErrorActionPreference，失败仅 warn 不中断。
    """

    def test_docker_restart_wraps_error_action_preference(self):
        content = _read_file("update_frontend.ps1")
        assert "docker restart" in content, "前提：脚本应含 docker restart 调用"
        lines = content.splitlines()
        # 定位 docker restart 所在行
        restart_idx = next(
            (i for i, ln in enumerate(lines) if "docker restart" in ln),
            None,
        )
        assert restart_idx is not None, "未找到 docker restart 行"
        # 检查 ±8 行窗口内是否有 ErrorActionPreference 切到 Continue
        window = lines[max(0, restart_idx - 8): restart_idx + 9]
        window_text = "\n".join(window)
        assert "ErrorActionPreference" in window_text and "Continue" in window_text, (
            "docker restart 附近（±8行）未切换 ErrorActionPreference 到 Continue，"
            "docker.exe 写 stderr 会触发 NativeCommandError 终止脚本"
        )


# ---------------------------------------------------------------------------
# 5. 容器名冲突自动自愈（孤儿容器残留场景）
# ---------------------------------------------------------------------------
class TestOrphanedContainerNameConflict:
    """验证 compose up 遇"容器名已被占用"时自动清理孤儿容器后重试。

    场景：WSL --shutdown 重启内核后，旧容器进程死了，但 docker 元数据库里
    仍记录着同名容器（b4e0b7...）。compose up 重建时报：
        Error when allocating new name: Conflict. The container name
        "/container_224042d3b925-frontend-1" is already in use by container "b4e0..."
    这是用户态可自愈的（docker rm -f 孤儿即可），不应升级到 wsl --shutdown（exit 7），
    也不应直接放弃（exit 1）。脚本应识别此特征 → rm -f 冲突容器 → 重试 compose up。
    """

    def test_script_recognizes_name_conflict_pattern(self):
        """脚本应能识别 "already in use" / "Conflict" 名字冲突特征"""
        content = _read_file("wsl_ensure_containers.sh")
        assert "already in use" in content, (
            "脚本未识别容器名冲突特征（already in use），"
            "孤儿容器残留时会直接 exit 1 放弃而非自动清理"
        )

    def test_script_auto_removes_orphan_on_conflict(self):
        """识别到名字冲突后,脚本应在冲突处理分支里执行 docker rm -f。

        精确判断：存在一行同时含 already in use 上下文与 docker rm -f，
        或名字冲突识别与 docker rm -f 出现在同一个处理块（±10 行窗口）内。
        泛泛的"脚本任意位置有 docker rm -f"不算（清理 cua- 任务容器的地方也有）。
        """
        content = _read_file("wsl_ensure_containers.sh")
        lines = content.splitlines()
        # 定位名字冲突识别行（含 already in use 的行）
        conflict_idx = next(
            (i for i, ln in enumerate(lines) if "already in use" in ln),
            None,
        )
        assert conflict_idx is not None, (
            "应存在识别 already in use 名字冲突的逻辑行"
        )
        # 在冲突识别行之后的处理窗口（±10 行）内应有 docker rm -f
        window = lines[conflict_idx:min(len(lines), conflict_idx + 11)]
        window_text = "\n".join(window)
        assert "docker rm -f" in window_text, (
            "名字冲突识别后（后续 10 行内）应执行 docker rm -f 清理孤儿容器，"
            "而非依赖别处的 cua- 清理逻辑"
        )

    def test_conflict_triggers_retry_not_shutdown(self):
        """名字冲突属于可自愈场景,不应走 exit 7 (wsl --shutdown) 分支。

        验证：名字冲突的处理逻辑应在 exit 7 死锁分支之后（即优先级更低），
        或者死锁 grep 特征不含名字冲突关键词。
        """
        content = _read_file("wsl_ensure_containers.sh")
        # 死锁 grep 的特征正则不应误含名字冲突关键词（否则会把可自愈问题升级为重启内核）
        # 提取死锁 grep 那一行
        deadlock_grep_lines = [
            ln for ln in content.splitlines()
            if "grep" in ln and "did not receive an exit event" in ln
        ]
        assert deadlock_grep_lines, "应存在含 did not receive an exit event 的死锁 grep 行"
        deadlock_pattern = deadlock_grep_lines[0]
        # 名字冲突关键词不得出现在死锁特征里（否则误判）
        assert "already in use" not in deadlock_pattern, (
            "名字冲突(已存在孤儿容器)是用户态可自愈的,不应被归入死锁特征(exit 7 会无谓重启内核)"
        )
