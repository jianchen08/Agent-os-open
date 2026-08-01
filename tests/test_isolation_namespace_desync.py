"""runc setns 命名空间脱节自动恢复测试。

背景 BUG：
docker exec 报 `OCI runtime exec failed: exec failed: unable to start
container process: error executing setns process: exit status 1`。此时 runc
的容器命名空间已死，但 `docker inspect` 仍报 `running`——现有 pre-exec 健康
检查（_ensure_env_healthy_or_rebuild 只看 inspect 的 Status 字段）永远放行，
错误在 exec 时原样冒泡成失败的 ExecutionResult，无任何重试。

修复（见 src/isolation/providers/docker_provider.py、src/isolation/manager.py）：
- 新增 _is_namespace_desync_error：识别 runc setns 脱节特征字符串，与"命令
  本身的 stderr"区分——后者重试无意义，前者重建容器通常即恢复。
- execute_in_isolation：post-exec 命中 setns 脱节 → destroy + 重建 + 单次
  重试（不计熔断，与可恢复瞬时故障语义一致）。

本测试为标记识别纯函数的参数化用例（集成层见
test_isolation_container_self_heal.py）。
"""

from __future__ import annotations

import pytest

from isolation.providers.docker_provider import DockerProvider

# ---------------------------------------------------------------------------
# 真实事故样本（取自 healthcheck 日志原样输出）→ 应识别为 True
# ---------------------------------------------------------------------------

_REAL_DESYNC_SAMPLES = [
    # 实测 healthcheck 日志原样（docker exec 在坏容器上的完整 stderr）
    "OCI runtime exec failed: exec failed: unable to start container process: "
    "error executing setns process: exit status 1",
    # 仅 setns 子串
    "error executing setns process: exit status 1",
    # docker exec 输出常带前缀
    "time=\"2026-07-15T21:14:33Z\" OCI runtime exec failed: exec failed: "
    "unable to start container process: error executing setns process: "
    "exit status 1\n",
    # 不同 runc 版本的变体
    "docker: Error response from daemon: OCI runtime exec failed: "
    "unable to start container process: exec: \"sh\": executable file not found",
]

# ---------------------------------------------------------------------------
# 非命名空间脱节的样本（命令本身的 stderr / 其它失败），不应误判
# ---------------------------------------------------------------------------

_NON_DESYNC_SAMPLES = [
    # 普通命令失败 stderr
    "sh: command not found: foo",
    "error: use of undeclared identifier 'bar'",
    # apt/pip 网络超时
    "E: Failed to fetch http://deb.debian.org/... Could not connect",
    # 容器内进程返回非零（命令语义错误，非 runc 故障）
    "exit code 1",
    "Command '['python', 'bad.py']' returned non-zero exit status 1",
    # 空字符串
    "",
    # "is not running" 是另一类故障（pre-exec 健康检查已覆盖），此处不归 setns
    "Error response from daemon: Container abc is not running",
    # BuildKit 缓存损坏（属另一套自愈标记，不应被 setns 误判）
    "unpigz: skipping: <stdin>: corrupted -- crc32 mismatch",
]


@pytest.mark.parametrize("sample", _REAL_DESYNC_SAMPLES)
def test_detects_real_namespace_desync_samples(sample: str):
    """真实事故里的 setns 脱节样本必须被识别为 True。"""
    assert DockerProvider._is_namespace_desync_error(sample) is True, (
        f"应识别为命名空间脱节但未识别: {sample!r}"
    )


@pytest.mark.parametrize("sample", _NON_DESYNC_SAMPLES)
def test_does_not_false_positive(sample: str):
    """非 setns 脱节的失败不应被误判（否则会无谓重建容器）。"""
    assert DockerProvider._is_namespace_desync_error(sample) is False, (
        f"不应识别为命名空间脱节但误判了: {sample!r}"
    )


def test_case_insensitive():
    """检测应大小写不敏感（runc/docker 输出大小写不稳定）。"""
    assert DockerProvider._is_namespace_desync_error(
        "oci runtime EXEC FAILED: ... Error Executing SetNs"
    ) is True


def test_accepts_bytes():
    """stderr 通常是 bytes，应自动解码处理。"""
    sample = (
        b"OCI runtime exec failed: exec failed: unable to start container "
        b"process: error executing setns process: exit status 1\n"
    )
    assert DockerProvider._is_namespace_desync_error(sample) is True


def test_accepts_none():
    """result.error 可能是 None（success=True 时），应安全返回 False。"""
    assert DockerProvider._is_namespace_desync_error(None) is False
