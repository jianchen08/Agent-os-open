"""9p/drvfs EIO（Input/output error）标记识别测试。

背景 BUG：
WSL docker 模式下，容器通过 bind mount 访问宿主 /mnt/<盘>（9p 协议桥接
Windows NTFS）。高负载 + VM 异常终止会让 9p 通道状态机损坏，容器内访问
bind mount 路径时报 `Input/output error (EIO)`。此时 docker inspect 仍报
running，pre-exec 健康检查放行，错误只在 exec 时冒泡成失败的
ExecutionResult。现有 setns 自愈（_is_namespace_desync_error）的 marker
不含 EIO 文本，故 EIO 走普通失败路径，不触发自愈。

修复（见 src/isolation/providers/docker_provider.py、src/isolation/manager.py）：
- 新增 _is_io_error：识别 9p/drvfs EIO 特征字符串，与"命令本身的 stderr"
  （ENOENT/EACCES 等）区分——后者重试无意义，前者修宿主挂载+重建容器
  通常即恢复。
- execute_in_isolation：post-exec 命中 EIO → 修宿主挂载（umount+mount）
  + destroy + 重建 + 单次重试（与 setns 自愈同构，但多了宿主修复步骤）。

本测试为标记识别纯函数的参数化用例（集成层见
test_isolation_container_self_heal.py）。
"""

from __future__ import annotations
import tests._isolation_path  # noqa: F401

import pytest

from providers.docker_provider import DockerProvider

# ---------------------------------------------------------------------------
# 真实事故样本（取自 agent 任务报错原样输出）→ 应识别为 True
# ---------------------------------------------------------------------------

_REAL_EIO_SAMPLES = [
    # 实测报错原样（agent 在容器内 ls worktree 文件）
    "ls: cannot access '/workspace/docs/working/programming_orchestration_report.md': Input/output error",
    # stat 报错
    "stat: cannot statx '/workspace': Input/output error",
    # cat/读文件 EIO
    "cat: /workspace/README.md: Input/output error",
    # 仅 EIO 子串
    "Input/output error",
    # ls 目录 EIO（用户首次报告的原样）
    "ls: cannot access '/workspace/plugins/': Input/output error",
]

# ---------------------------------------------------------------------------
# 非 EIO 的样本（命令本身的 stderr / 其它确定性失败），不应误判
# ---------------------------------------------------------------------------

_NON_EIO_SAMPLES = [
    # 普通命令失败 stderr
    "sh: command not found: foo",
    "ls: cannot access '/workspace/no_such_file': No such file or directory",
    # 权限拒绝
    "bash: /workspace/script.sh: Permission denied",
    # apt/pip 网络超时
    "E: Failed to fetch http://deb.debian.org/... Could not connect",
    # 容器内进程返回非零（命令语义错误，非 IO 故障）
    "exit code 1",
    # 空字符串
    "",
    # setns 命名空间脱节（属另一套自愈标记，不应被 EIO 误判，反之亦然）
    "OCI runtime exec failed: exec failed: unable to start container process: "
    "error executing setns process: exit status 1",
    # BuildKit 缓存损坏（属另一套自愈标记）
    "unpigz: skipping: <stdin>: corrupted -- crc32 mismatch",
]


@pytest.mark.parametrize("sample", _REAL_EIO_SAMPLES)
def test_detects_real_eio_samples(sample: str):
    """真实事故里的 EIO 样本必须被识别为 True。"""
    assert DockerProvider._is_io_error(sample) is True, (
        f"应识别为 EIO 但未识别: {sample!r}"
    )


@pytest.mark.parametrize("sample", _NON_EIO_SAMPLES)
def test_does_not_false_positive(sample: str):
    """非 EIO 的失败不应被误判（否则会无谓重建容器+重挂宿主）。"""
    assert DockerProvider._is_io_error(sample) is False, (
        f"不应识别为 EIO 但误判了: {sample!r}"
    )


def test_case_insensitive():
    """检测应大小写不敏感（coreutils/docker 输出大小写不稳定）。"""
    assert DockerProvider._is_io_error("INPUT/OUTPUT ERROR") is True
    assert DockerProvider._is_io_error("input/Output Error") is True


def test_accepts_bytes():
    """stderr 通常是 bytes，应自动解码处理。"""
    sample = b"ls: cannot access '/workspace/foo': Input/output error\n"
    assert DockerProvider._is_io_error(sample) is True


def test_accepts_none():
    """result.error 可能是 None（success=True 时），应安全返回 False。"""
    assert DockerProvider._is_io_error(None) is False


def test_markers_disjoint_from_namespace_desync():
    """EIO marker 与 setns marker 互不重叠（确保两类自愈不会互相吞掉）。"""
    eio_sample = "ls: cannot access '/workspace/x': Input/output error"
    setns_sample = (
        "OCI runtime exec failed: error executing setns process: exit status 1"
    )
    # EIO 不应被 setns 判定命中
    assert DockerProvider._is_namespace_desync_error(eio_sample) is False
    # setns 不应被 EIO 判定命中
    assert DockerProvider._is_io_error(setns_sample) is False
