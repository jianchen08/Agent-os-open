"""BuildKit 缓存损坏自动恢复测试。

背景 BUG（Q1）：
docker build 偶发报 `unpigz: skipping: <stdin>: corrupted -- crc32 mismatch`，
这是 BuildKit 缓存层损坏（非 Dockerfile 内容问题）。但 _ensure_image 当时
一次 build 失败就直接 raise RuntimeError，manager 立即计入失败计数（_MAX_ENV_FAILURES=3），
三次就熔断——把"可恢复的瞬时故障"当成了"环境不可用"。

修复（见 src/isolation/providers/docker_provider.py）：
- 新增 _is_buildkit_cache_corruption：识别缓存损坏特征字符串。
- _ensure_image：build 失败若判定为缓存损坏，自动 `docker builder prune -f`
  后重试一次 build；第二次还失败才 raise。重试是自愈，不该频繁触发。

本测试分两组：
1. _is_buildkit_cache_corruption 纯函数：喂真实 stderr 样本断言 True/False。
2. _ensure_image 集成：mock _run_cmd，验证缓存损坏时触发 prune + 重试。
"""

from __future__ import annotations

import pytest

from isolation.providers.docker_provider import DockerProvider

# ---------------------------------------------------------------------------
# 1. _is_buildkit_cache_corruption：缓存损坏特征识别
# ---------------------------------------------------------------------------

# 真实事故 stderr 样本（取自 Q1 复盘）
_REAL_CORRUPTION_SAMPLES = [
    # 原文：unpigz: skipping: <stdin>: corrupted -- crc32 mismatch
    "ERROR: unpigz: skipping: <stdin>: corrupted -- crc32 mismatch\n",
    # 仅 unpigz + corrupted
    "unpigz: skipping: corrupted\n",
    # checksum 类
    "failed to compute cache key: checksum mismatch\n",
    "ERROR verifying layer: crc32 mismatch\n",
]

# 非缓存损坏的失败样本（Dockerfile 语法错、网络超时、磁盘满等），不应误判
_NON_CORRUPTION_SAMPLES = [
    # Dockerfile 语法错误
    "dockerfile parse error on line 5: unknown instruction: RUNN",
    # apt 网络超时
    "E: Failed to fetch http://deb.debian.org/debian/pool/main/... Could not connect",
    # 磁盘满
    "write /var/lib/docker: no space left on device",
    # 镜像不存在
    "manifest unknown",
    # 空字符串
    "",
    # 普通 build 失败（编译错）
    "error: use of undeclared identifier 'foo'",
]


@pytest.mark.parametrize("sample", _REAL_CORRUPTION_SAMPLES)
def test_detects_real_corruption_samples(sample: str):
    """真实事故里的缓存损坏样本必须被识别为 True。"""
    assert DockerProvider._is_buildkit_cache_corruption(sample) is True, f"应识别为缓存损坏但未识别: {sample!r}"


@pytest.mark.parametrize("sample", _NON_CORRUPTION_SAMPLES)
def test_does_not_false_positive(sample: str):
    """非缓存损坏的 build 失败不应被误判为缓存损坏。"""
    assert DockerProvider._is_buildkit_cache_corruption(sample) is False, f"不应识别为缓存损坏但误判了: {sample!r}"


def test_case_insensitive():
    """检测应大小写不敏感（BuildKit 输出大小写不稳定）。"""
    assert DockerProvider._is_buildkit_cache_corruption("UNPIGZ: CORRUPTED") is True
    assert DockerProvider._is_buildkit_cache_corruption("CRC32 Mismatch") is True


def test_accepts_bytes():
    """stderr 通常是 bytes，应自动解码处理。"""
    sample = b"unpigz: skipping: <stdin>: corrupted -- crc32 mismatch\n"
    assert DockerProvider._is_buildkit_cache_corruption(sample) is True


# ---------------------------------------------------------------------------
# 2. _ensure_image：缓存损坏触发自动 prune + 重试一次
# ---------------------------------------------------------------------------


def _make_provider(tmp_path) -> DockerProvider:
    """构造一个指向临时 Dockerfile 的 DockerProvider。"""
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.11-slim\n", encoding="utf-8")
    return DockerProvider(
        config={
            "image": "test-agentos:latest",
            "auto_build": True,
            "dockerfile_path": str(dockerfile),
            "build_context": str(tmp_path),
        }
    )


@pytest.mark.asyncio
async def test_ensure_image_prunes_and_retries_on_cache_corruption(tmp_path):
    """build 报缓存损坏 → 自动 prune + 重试一次，重试成功则整体成功。"""
    provider = _make_provider(tmp_path)

    call_log: list[str] = []

    async def fake_run(args, timeout=30, env=None, stream_log=False):
        sub = args[1]
        call_log.append(sub)
        if sub == "image" and args[2] == "inspect":
            # 镜像不存在（触发构建）
            return 1, b"", b"no such image"
        if sub == "build":
            # 第一次 build 失败（缓存损坏），第二次成功
            builds = [c for c in call_log if c == "build"]
            if len(builds) == 1:
                return 1, b"", b"unpigz: skipping: <stdin>: corrupted -- crc32 mismatch"
            return 0, b"build ok", b""
        if sub == "builder" and args[2] == "prune":
            return 0, b"pruned", b""
        return 0, b"", b""

    provider._run_cmd = fake_run  # type: ignore[method-assign]

    # 不应 raise（重试后成功）
    await provider._ensure_image()

    # 必须调用了 builder prune（args[1]='builder'，args[2]='prune'）
    assert "builder" in call_log, f"缓存损坏未触发 builder prune，调用序列: {call_log}"
    # build 必须被调了 2 次（首次失败 + 重试）
    assert call_log.count("build") == 2, f"build 应重试一次，调用序列: {call_log}"
    # prune 必须在两次 build 之间
    first_build = call_log.index("build")
    second_build = call_log.index("build", first_build + 1)
    prune_idx = call_log.index("builder")
    assert first_build < prune_idx < second_build, f"builder prune 应在两次 build 之间，序列: {call_log}"


@pytest.mark.asyncio
async def test_ensure_image_raises_after_retry_still_fails(tmp_path):
    """缓存损坏重试后仍失败 → 必须 raise（交给 manager 熔断器）。"""
    provider = _make_provider(tmp_path)

    async def fake_run(args, timeout=30, env=None, stream_log=False):
        sub = args[1]
        if sub == "image" and args[2] == "inspect":
            return 1, b"", b"no such image"
        if sub == "build":
            # 两次都失败（持续损坏）
            return 1, b"", b"unpigz: corrupted -- crc32 mismatch"
        if sub == "builder" and args[2] == "prune":
            return 0, b"pruned", b""
        return 0, b"", b""

    provider._run_cmd = fake_run  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="自动构建失败"):
        await provider._ensure_image()


@pytest.mark.asyncio
async def test_ensure_image_no_retry_for_non_corruption_failure(tmp_path):
    """非缓存损坏的 build 失败 → 不 prune、不重试，直接 raise。"""
    provider = _make_provider(tmp_path)

    call_log: list[str] = []

    async def fake_run(args, timeout=30, env=None, stream_log=False):
        sub = args[1]
        call_log.append(sub)
        if sub == "image" and args[2] == "inspect":
            return 1, b"", b"no such image"
        if sub == "build":
            # Dockerfile 语法错，不是缓存损坏
            return 1, b"", b"dockerfile parse error on line 5: unknown instruction"
        return 0, b"", b""

    provider._run_cmd = fake_run  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="自动构建失败"):
        await provider._ensure_image()

    # build 只调一次，prune 不该被调
    assert call_log.count("build") == 1, f"非缓存损坏不应重试，序列: {call_log}"
    assert "builder" not in call_log, f"非缓存损坏不应 prune，序列: {call_log}"
