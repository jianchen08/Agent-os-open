# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""ASR 语音识别服务单元测试。

覆盖场景：
- ASRConfig 默认值
- ASRService.is_available（启用/未启用/key 缺失）
- transcribe 成功路径（mock aiohttp，验证 multipart 上传与 text 解析）
- transcribe HTTP 错误抛 RuntimeError
- transcribe 未配置时抛 RuntimeError
- transcribe 响应缺少文本抛 RuntimeError
- load_asr_config 环境变量回退（asr.yaml 缺失时读 ZHIPU_API_KEY）
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 0.2：multimodal 插件位于 plugins/shared/system/multimodal/，
# from multimodal.asr import 需要 system/ 父目录（包命名空间）；
# asr.py 内部平铺导入 mm_types（multimodal 目录自身）与 user_space
# （plugins/shared 裸模块，配置用户层解析）。
_SHARED = Path(__file__).resolve().parent.parent / "plugins" / "shared"
_SYSTEM = _SHARED / "system"
_MULTIMODAL = _SYSTEM / "multimodal"
for _p in (_SHARED, _SYSTEM, _MULTIMODAL):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from multimodal.asr import (  # noqa: E402
    ASRConfig,
    ASRService,
    load_asr_config,
    reset_asr_service,
)


def _async_run(coro):
    """安全执行 async 函数（兼容已有事件循环）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor() as pool:
        return pool.submit(asyncio.run, coro).result(timeout=10)


def test_default_config():
    """ASRConfig 应使用默认值。"""
    cfg = ASRConfig()
    assert cfg.model == "glm-asr-v1"
    assert cfg.language == "zh-CN"
    assert cfg.timeout == 60


def test_is_available_with_key():
    """配置了 api_key 且 enabled 时应可用。"""
    svc = ASRService(ASRConfig(api_key="sk-test", enabled=True))
    assert svc.is_available() is True


def test_not_available_without_key():
    """缺少 api_key 时不可用。"""
    svc = ASRService(ASRConfig(api_key="", enabled=True))
    assert svc.is_available() is False


def test_not_available_when_disabled():
    """enabled=False 时不可用。"""
    svc = ASRService(ASRConfig(api_key="sk-test", enabled=False))
    assert svc.is_available() is False


def _make_mock_response(status: int, json_data: dict | None = None):
    """构造 aiohttp 响应 mock（支持 async with）。"""
    resp = AsyncMock()
    resp.status = status
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    resp.text = AsyncMock(return_value=str(json_data))
    # async with session.post(...) as resp 的上下文管理协议
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _patch_client_session(mock_resp):
    """patch aiohttp.ClientSession，使其 post 返回 mock_resp 的 async context。

    覆盖 ``async with ClientSession() as session, session.post(...) as resp`` 结构。
    """
    mock_post_cm = MagicMock()
    mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_post_cm.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_post_cm)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    def fake_session_ctor():
        return mock_session

    return patch("multimodal.asr.aiohttp.ClientSession", side_effect=fake_session_ctor), mock_session


def test_transcribe_success():
    """成功转写：应返回响应中的 text 字段。"""
    svc = ASRService(ASRConfig(api_key="sk-test", api_base="https://example.com/v1"))
    mock_resp = _make_mock_response(200, {"text": "你好世界"})
    patcher, mock_session = _patch_client_session(mock_resp)

    with patcher:
        text = _async_run(svc.transcribe(b"fake-audio", "audio/webm"))

    assert text == "你好世界"
    # 验证请求 URL
    called_url = mock_session.post.call_args[0][0]
    assert called_url == "https://example.com/v1/audio/transcriptions"


def test_transcribe_http_error():
    """HTTP 非 200 应抛 RuntimeError。"""
    svc = ASRService(ASRConfig(api_key="sk-test"))
    mock_resp = _make_mock_response(401, {"error": "unauthorized"})
    patcher, _mock_session = _patch_client_session(mock_resp)

    with patcher, pytest.raises(RuntimeError, match="status=401"):
        _async_run(svc.transcribe(b"fake-audio", "audio/webm"))


def test_transcribe_not_configured():
    """未配置时转写应抛 RuntimeError。"""
    svc = ASRService(ASRConfig(api_key="", enabled=False))
    with pytest.raises(RuntimeError, match="未配置"):
        _async_run(svc.transcribe(b"fake-audio", "audio/webm"))


def test_transcribe_empty_audio():
    """空音频应抛 ValueError。"""
    svc = ASRService(ASRConfig(api_key="sk-test"))
    with pytest.raises(ValueError, match="不能为空"):
        _async_run(svc.transcribe(b"", "audio/webm"))


def test_transcribe_missing_text():
    """响应缺少 text 字段应抛 RuntimeError。"""
    svc = ASRService(ASRConfig(api_key="sk-test"))
    mock_resp = _make_mock_response(200, {"unrelated": "data"})
    patcher, _mock_session = _patch_client_session(mock_resp)

    with patcher, pytest.raises(RuntimeError, match="缺少转写文本"):
        _async_run(svc.transcribe(b"fake-audio", "audio/webm"))


def test_load_config_env_fallback(tmp_path, monkeypatch):
    """asr.yaml 缺失时，应从 ZHIPU_API_KEY 环境变量回退。"""
    monkeypatch.setenv("ZHIPU_API_KEY", "sk-from-env")
    cfg = load_asr_config(tmp_path / "nonexistent.yaml")
    assert cfg.api_key == "sk-from-env"
    assert cfg.enabled is True


def test_load_config_missing_env(tmp_path, monkeypatch):
    """asr.yaml 缺失且环境变量未设置时，enabled 应为 False。"""
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    cfg = load_asr_config(tmp_path / "nonexistent.yaml")
    assert cfg.api_key == ""
    assert cfg.enabled is False


def test_get_asr_service_singleton():
    """get_asr_service 应返回单例。"""
    reset_asr_service()
    svc1 = ASRService.__new__(ASRService)
    svc1._config = ASRConfig(api_key="sk-test")
    with patch("multimodal.asr._asr_service", svc1):
        svc2 = ASRService.__new__(ASRService)
        svc2._config = ASRConfig(api_key="sk-test")
        # 单例已缓存时，应返回缓存的实例
        from multimodal import asr as asr_mod

        asr_mod._asr_service = svc1
        assert asr_mod.get_asr_service() is svc1
    reset_asr_service()


# ── 用户空间真值 + 配置热生效（设置中枢 ASR 配置页的读取契约）──────────────


def _user_yaml(model: str, api_key: str = "sk-user") -> str:
    return (
        "asr:\n"
        "  enabled: true\n"
        "  default_provider: p1\n"
        "  providers:\n"
        "    p1:\n"
        '      api_base: "https://user.example.com/v1"\n'
        f'      api_key: "{api_key}"\n'
        f'      model: "{model}"\n'
        '      language: "zh-CN"\n'
    )


def _clear_user_space_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)


def test_load_config_user_layer_priority(tmp_path, monkeypatch):
    """用户层存在 models/asr.yaml 时优先于工厂配置（文件级整体替换，不合并）。"""
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path))
    user_yaml = tmp_path / "models" / "asr.yaml"
    user_yaml.parent.mkdir(parents=True)
    user_yaml.write_text(_user_yaml("user-asr-model"), encoding="utf-8")

    cfg = load_asr_config()

    assert cfg.model == "user-asr-model"
    assert cfg.api_key == "sk-user"
    assert cfg.api_base == "https://user.example.com/v1"


def test_load_config_factory_fallback_without_user_layer(monkeypatch):
    """用户层未接管（文件不存在）时回落工厂 config/models/asr.yaml。"""
    _clear_user_space_env(monkeypatch)

    cfg = load_asr_config()

    assert cfg.model == "glm-asr-v1"


def test_service_picks_up_user_layer_created_later(tmp_path, monkeypatch):
    """设置页首次保存（用户层文件从无到有）：既有服务实例下次读取即切用户层。"""
    _clear_user_space_env(monkeypatch)
    svc = ASRService()
    assert svc.config.model == "glm-asr-v1"

    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path))
    user_yaml = tmp_path / "models" / "asr.yaml"
    user_yaml.parent.mkdir(parents=True)
    user_yaml.write_text(_user_yaml("late-user-model"), encoding="utf-8")

    assert svc.config.model == "late-user-model"
    assert svc.is_available() is True


def test_service_reloads_on_file_content_change(tmp_path, monkeypatch):
    """配置文件内容变更（stat 签名变化）后，既有实例下次读取新值（免重启热生效）。"""
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path))
    yaml_path = tmp_path / "models" / "asr.yaml"
    yaml_path.parent.mkdir(parents=True)
    yaml_path.write_text(_user_yaml("m1", api_key="k1"), encoding="utf-8")

    svc = ASRService()
    assert svc.config.model == "m1"
    assert svc.config.api_key == "k1"

    yaml_path.write_text(_user_yaml("m2", api_key="k2"), encoding="utf-8")
    # Windows mtime 粒度粗：显式推进 stat 签名，测试不受文件系统时间分辨率影响
    st = yaml_path.stat()
    os.utime(yaml_path, ns=(st.st_atime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000))

    assert svc.config.model == "m2"
    assert svc.config.api_key == "k2"
    assert svc.is_available() is True


def test_service_explicit_config_not_reloaded(tmp_path, monkeypatch):
    """显式传入 ASRConfig（代码内构造，无配置文件语义）时不做文件重载探测。"""
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path))
    user_yaml = tmp_path / "models" / "asr.yaml"
    user_yaml.parent.mkdir(parents=True)
    user_yaml.write_text(_user_yaml("user-model"), encoding="utf-8")

    svc = ASRService(ASRConfig(api_key="sk-inline", model="inline-model"))
    assert svc.config.model == "inline-model"
    assert svc.config.api_key == "sk-inline"


def test_stat_signature_missing_file_returns_none(tmp_path):
    """不可 stat（文件缺失）时签名返回 None，调用方保持现值不重载。"""
    from multimodal import asr as asr_mod

    assert asr_mod._stat_signature(tmp_path / "missing.yaml") is None
