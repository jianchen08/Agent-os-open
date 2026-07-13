"""企业微信通道适配器单元测试。

覆盖：
- WecomCrypto: 消息加解密和签名验证
- WeComStreamClient: HTTP 客户端和 access_token 管理
- WeComAdapter: 组合模式、回调处理
- MessageNormalizer: wecom 标准化器注册和转换
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import time
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.gateway.message_normalizer import MessageNormalizer
from channels.gateway.unified_types import UnifiedMessage, UnifiedResponse
from channels.wecom.adapter import (
    WeComAdapter,
    WeComInputAdapter,
    WeComOutputAdapter,
    _extract_wecom_text,
)
from channels.wecom.crypto import WecomCrypto
from channels.wecom.stream_client import WeComStreamClient

# ── 测试用常量 ──────────────────────────────────────

# 生成有效的 43 字符 EncodingAESKey（32 字节 AES 密钥的 Base64 编码去掉尾部 '='）
_TEST_AES_KEY = base64.b64encode(os.urandom(32)).decode("utf-8").rstrip("=")[:43]
# 补齐 Base64
_FULL_KEY = _TEST_AES_KEY + "="
_DECODED_KEY = base64.b64decode(_FULL_KEY)

TOKEN = "test_token_abc123"
CORP_ID = "ww1234567890abcdef"
AGENT_ID = 1000001
SECRET = "test_secret_xyz789"
ENCODING_AES_KEY = _TEST_AES_KEY


# ── WecomCrypto 测试 ──────────────────────────────────


class TestWecomCrypto:
    """WecomCrypto 加解密测试。"""

    def setup_method(self) -> None:
        """每个测试前创建 crypto 实例。"""
        self.crypto = WecomCrypto(
            token=TOKEN,
            encoding_aes_key=ENCODING_AES_KEY,
            corp_id=CORP_ID,
        )

    def test_init_decodes_aes_key(self) -> None:
        """初始化时正确解码 EncodingAESKey。"""
        assert self.crypto._aes_key == _DECODED_KEY
        assert self.crypto._iv == _DECODED_KEY[:16]
        assert len(self.crypto._aes_key) == 32

    def test_verify_signature_valid(self) -> None:
        """有效签名验证通过。"""
        timestamp = "1234567890"
        nonce = "test_nonce"
        msg_encrypt = "encrypted_content"

        # 计算正确签名
        parts = sorted([TOKEN, timestamp, nonce, msg_encrypt])
        raw = "".join(parts)
        signature = hashlib.sha1(raw.encode("utf-8")).hexdigest()

        assert self.crypto.verify_signature(timestamp, nonce, msg_encrypt, signature) is True

    def test_verify_signature_invalid(self) -> None:
        """无效签名验证失败。"""
        assert self.crypto.verify_signature("1234567890", "nonce", "encrypt", "wrong_signature") is False

    def test_encrypt_and_decrypt_roundtrip(self) -> None:
        """加密-解密往返测试。"""
        original_msg = "<xml><Content>Hello WeCom!</Content></xml>"

        # 加密
        encrypted_xml = self.crypto.encrypt_response(original_msg)
        assert "<Encrypt>" in encrypted_xml
        assert "<MsgSignature>" in encrypted_xml

        # 解密
        decrypted = self.crypto.decrypt_message(encrypted_xml)
        assert decrypted == original_msg

    def test_decrypt_echo(self) -> None:
        """解密回调 URL 验证的 echostr。"""
        echo_str = "1234567890"
        # 手动构造加密内容
        random_bytes = os.urandom(16)
        msg_bytes = echo_str.encode("utf-8")
        msg_len = struct.pack("!I", len(msg_bytes))
        corp_id_bytes = CORP_ID.encode("utf-8")
        plaintext = random_bytes + msg_len + msg_bytes + corp_id_bytes

        from cryptography.hazmat.primitives import padding as sym_padding  # noqa: PLC0415
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: PLC0415

        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()

        cipher = Cipher(algorithms.AES(_DECODED_KEY), modes.CBC(_DECODED_KEY[:16]))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        encrypted_b64 = base64.b64encode(ciphertext).decode("utf-8")

        result = self.crypto.decrypt_echo(encrypted_b64)
        assert result == echo_str

    def test_decrypt_message_corp_id_mismatch(self) -> None:
        """解密消息时 corp_id 不匹配抛出 ValueError。"""
        # 构造加密内容，但 corp_id 不匹配
        random_bytes = os.urandom(16)
        msg_bytes = b"test"
        msg_len = struct.pack("!I", len(msg_bytes))
        wrong_corp = b"wrong_corp_id"
        plaintext = random_bytes + msg_len + msg_bytes + wrong_corp

        from cryptography.hazmat.primitives import padding as sym_padding  # noqa: PLC0415
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: PLC0415

        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()

        cipher = Cipher(algorithms.AES(_DECODED_KEY), modes.CBC(_DECODED_KEY[:16]))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        encrypted_b64 = base64.b64encode(ciphertext).decode("utf-8")

        # 包装为 XML
        xml_str = f"<xml><Encrypt><![CDATA[{encrypted_b64}]]></Encrypt></xml>"

        with pytest.raises(ValueError, match="CorpID mismatch"):
            self.crypto.decrypt_message(xml_str)

    def test_decrypt_invalid_xml(self) -> None:
        """解密无效 XML 抛出 ValueError。"""
        with pytest.raises(ValueError):  # noqa: PT011
            self.crypto.decrypt_message("not valid xml")


# ── WeComStreamClient 测试 ──────────────────────────────────


class TestWeComStreamClient:
    """WeComStreamClient HTTP 客户端测试。"""

    def test_init(self) -> None:
        """初始化参数正确设置。"""
        client = WeComStreamClient(
            corp_id=CORP_ID,
            agent_id=AGENT_ID,
            secret=SECRET,
        )
        assert client._corp_id == CORP_ID
        assert client._agent_id == AGENT_ID
        assert client._secret == SECRET
        assert client.on_message is None

    def test_is_connected_false_before_connect(self) -> None:
        """连接前 is_connected 为 False。"""
        client = WeComStreamClient(corp_id=CORP_ID, agent_id=AGENT_ID, secret=SECRET)
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_connect_creates_session(self) -> None:
        """connect 创建 HTTP 会话。"""
        client = WeComStreamClient(corp_id=CORP_ID, agent_id=AGENT_ID, secret=SECRET)
        with patch.object(client, "_ensure_token", new_callable=AsyncMock):
            await client.connect()
        assert client._session is not None
        assert client.is_connected is True
        await client._session.close()

    @pytest.mark.asyncio
    async def test_disconnect_closes_session(self) -> None:
        """disconnect 关闭 HTTP 会话。"""
        client = WeComStreamClient(corp_id=CORP_ID, agent_id=AGENT_ID, secret=SECRET)
        with patch.object(client, "_ensure_token", new_callable=AsyncMock):
            await client.connect()
        await client.disconnect()
        assert client._session is None

    @pytest.mark.asyncio
    async def test_trigger_on_message_calls_callback(self) -> None:
        """trigger_on_message 触发注册的回调。"""
        client = WeComStreamClient(corp_id=CORP_ID, agent_id=AGENT_ID, secret=SECRET)
        callback = AsyncMock()
        client.on_message = callback

        msg = {"Content": "hello", "FromUserName": "user1"}
        await client.trigger_on_message(msg)

        callback.assert_awaited_once_with(msg)

    @pytest.mark.asyncio
    async def test_trigger_on_message_no_callback(self) -> None:
        """没有注册回调时 trigger_on_message 不报错。"""
        client = WeComStreamClient(corp_id=CORP_ID, agent_id=AGENT_ID, secret=SECRET)
        # 不应抛出异常
        await client.trigger_on_message({"Content": "test"})

    @pytest.mark.asyncio
    async def test_send_message_calls_api(self) -> None:
        """send_message 调用企业微信 API 发送消息。"""
        client = WeComStreamClient(corp_id=CORP_ID, agent_id=AGENT_ID, secret=SECRET)

        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"errcode": 0, "errmsg": "ok"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        client._session = mock_session
        client._access_token = "test_token_123"
        client._token_expires = time.time() + 3600

        result = await client.send_message("user1", "Hello World")

        assert result["errcode"] == 0
        mock_session.post.assert_called_once()
        # 验证请求体
        call_args = mock_session.post.call_args
        body = call_args[1]["json"]
        assert body["touser"] == "user1"
        assert body["msgtype"] == "text"
        assert body["text"]["content"] == "Hello World"
        assert body["agentid"] == AGENT_ID

    @pytest.mark.asyncio
    async def test_send_message_markdown(self) -> None:
        """send_message 支持 markdown 类型。"""
        client = WeComStreamClient(corp_id=CORP_ID, agent_id=AGENT_ID, secret=SECRET)

        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"errcode": 0, "errmsg": "ok"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)

        client._session = mock_session
        client._access_token = "test_token"
        client._token_expires = time.time() + 3600

        await client.send_message("user1", "**Bold**", msg_type="markdown")

        call_args = mock_session.post.call_args
        body = call_args[1]["json"]
        assert body["msgtype"] == "markdown"
        assert body["markdown"]["content"] == "**Bold**"

    @pytest.mark.asyncio
    async def test_ensure_token_refreshes_when_expired(self) -> None:
        """access_token 过期时自动刷新。"""
        client = WeComStreamClient(corp_id=CORP_ID, agent_id=AGENT_ID, secret=SECRET)

        mock_response = AsyncMock()
        mock_response.json = AsyncMock(
            return_value={
                "access_token": "new_token",
                "expires_in": 7200,
                "errcode": 0,
            }
        )
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)

        client._session = mock_session
        client._access_token = ""
        client._token_expires = 0.0

        await client._ensure_token()
        assert client._access_token == "new_token"

    @pytest.mark.asyncio
    async def test_ensure_token_skips_when_valid(self) -> None:
        """access_token 有效时不刷新。"""
        client = WeComStreamClient(corp_id=CORP_ID, agent_id=AGENT_ID, secret=SECRET)
        client._access_token = "valid_token"
        client._token_expires = time.time() + 3600
        client._session = MagicMock()  # 不应被调用

        await client._ensure_token()
        assert client._access_token == "valid_token"


# ── WeComInputAdapter 测试 ──────────────────────────────────


class TestWeComInputAdapter:
    """WeComInputAdapter 输入适配器测试。"""

    @pytest.mark.asyncio
    async def test_enqueue_and_receive(self) -> None:
        """消息入队后能被 receive 取出并转换为 state。"""
        adapter = WeComInputAdapter()

        raw_msg = {
            "FromUserName": "user123",
            "ToUserName": CORP_ID,
            "MsgType": "text",
            "Content": "你好企业微信",
            "MsgId": "1234567890",
            "AgentID": str(AGENT_ID),
            "CreateTime": "1700000000",
        }

        await adapter.enqueue_message(raw_msg)
        state = await adapter.receive()

        assert state["user_input"] == "你好企业微信"
        assert state["_channel_type"] == "wecom"
        assert state["_channel_user_id"] == "user123"
        assert state["_raw_message"] == raw_msg

    @pytest.mark.asyncio
    async def test_raw_to_state_with_image(self) -> None:
        """图片消息正确转换。"""
        raw_msg = {
            "FromUserName": "user1",
            "MsgType": "image",
            "Content": "",
            "PicUrl": "http://example.com/img.jpg",
            "MsgId": "111",
        }
        state = WeComInputAdapter._raw_to_state(raw_msg)
        assert state["user_input"] == "http://example.com/img.jpg"

    @pytest.mark.asyncio
    async def test_raw_to_state_with_voice_recognition(self) -> None:
        """语音识别消息正确提取文本。"""
        raw_msg = {
            "FromUserName": "user1",
            "MsgType": "voice",
            "Content": "",
            "Recognition": "你好",
            "MsgId": "222",
        }
        state = WeComInputAdapter._raw_to_state(raw_msg)
        assert state["user_input"] == "你好"


# ── WeComOutputAdapter 测试 ──────────────────────────────────


class TestWeComOutputAdapter:
    """WeComOutputAdapter 输出适配器测试。"""

    @pytest.mark.asyncio
    async def test_send_normal_result(self) -> None:
        """正常结果通过 stream_client 发送。"""
        mock_client = AsyncMock(spec=WeComStreamClient)
        adapter = WeComOutputAdapter(mock_client)
        adapter.set_channel_user_id("user1")

        state = {"_channel_user_id": "user1", "raw_result": "Hello"}
        await adapter.send(state)

        mock_client.send_message.assert_awaited_once_with("user1", "Hello")

    @pytest.mark.asyncio
    async def test_send_error(self) -> None:
        """错误结果发送错误消息。"""
        mock_client = AsyncMock(spec=WeComStreamClient)
        adapter = WeComOutputAdapter(mock_client)
        adapter.set_channel_user_id("user1")

        state = {"_channel_user_id": "user1", "raw_error": "Something went wrong"}
        await adapter.send(state)

        mock_client.send_message.assert_awaited_once_with("user1", "❌ 错误: Something went wrong")

    @pytest.mark.asyncio
    async def test_send_no_user_id(self) -> None:
        """无 user_id 时不发送。"""
        mock_client = AsyncMock(spec=WeComStreamClient)
        adapter = WeComOutputAdapter(mock_client)

        state = {"raw_result": "Hello"}
        await adapter.send(state)

        mock_client.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_stream_accumulates_and_flushes(self) -> None:
        """流式输出累积文本，flush 时发送。"""
        mock_client = AsyncMock(spec=WeComStreamClient)
        adapter = WeComOutputAdapter(mock_client)
        adapter.set_channel_user_id("user1")

        # 累积
        await adapter.send_stream({"text": "Hello "})
        await adapter.send_stream({"text": "World"})
        mock_client.send_message.assert_not_awaited()

        # flush
        await adapter.send_stream({"text": "!", "flush": True})
        mock_client.send_message.assert_awaited_once_with("user1", "Hello World!")


# ── WeComAdapter 组合模式测试 ──────────────────────────────────


class TestWeComAdapter:
    """WeComAdapter 组合模式集成测试。"""

    def _make_adapter(self) -> WeComAdapter:
        """创建测试用适配器。"""
        return WeComAdapter(
            corp_id=CORP_ID,
            agent_id=AGENT_ID,
            secret=SECRET,
            token=TOKEN,
            encoding_aes_key=ENCODING_AES_KEY,
        )

    def test_channel_type(self) -> None:
        """channel_type 返回 'wecom'。"""
        adapter = self._make_adapter()
        assert adapter.channel_type == "wecom"

    def test_composition_pattern(self) -> None:
        """组合模式：包含 input_adapter、output_adapter、stream_client、crypto。"""
        adapter = self._make_adapter()
        assert isinstance(adapter.input_adapter, WeComInputAdapter)
        assert isinstance(adapter.output_adapter, WeComOutputAdapter)
        assert isinstance(adapter.stream_client, WeComStreamClient)
        assert isinstance(adapter.crypto, WecomCrypto)

    def test_callback_binding(self) -> None:
        """stream_client 的 on_message 绑定到 input_adapter 的队列。"""
        adapter = self._make_adapter()
        assert adapter.stream_client.on_message == adapter.input_adapter.enqueue_message

    @pytest.mark.asyncio
    async def test_start(self) -> None:
        """start 调用 stream_client.connect。"""
        adapter = self._make_adapter()
        with patch.object(adapter.stream_client, "connect", new_callable=AsyncMock):
            await adapter.start()

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        """stop 调用 stream_client.disconnect。"""
        adapter = self._make_adapter()
        with patch.object(adapter.stream_client, "disconnect", new_callable=AsyncMock):
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_handle_callback_with_valid_message(self) -> None:
        """handle_callback 正确处理有效回调消息。"""
        adapter = self._make_adapter()

        # 构造有效的加密消息
        original_xml = (
            "<xml>"
            "<ToUserName><![CDATA[wCorp]]></ToUserName>"
            "<FromUserName><![CDATA[user1]]></FromUserName>"
            "<CreateTime>1700000000</CreateTime>"
            "<MsgType><![CDATA[text]]></MsgType>"
            "<Content><![CDATA[Hello]]></Content>"
            "<MsgId>123456</MsgId>"
            "<AgentID>1000001</AgentID>"
            "</xml>"
        )

        encrypted_xml = adapter.crypto.encrypt_response(original_xml)

        # 从加密 XML 中提取参数
        root = ET.fromstring(encrypted_xml)
        root.find("Encrypt").text  # noqa: B018
        timestamp = root.find("TimeStamp").text
        nonce = root.find("Nonce").text
        signature = root.find("MsgSignature").text

        # 模拟 stream_client.trigger_on_message
        with patch.object(adapter.stream_client, "trigger_on_message", new_callable=AsyncMock) as mock_trigger:
            result = await adapter.handle_callback(timestamp, nonce, signature, encrypted_xml)
            assert "Hello" in result
            mock_trigger.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_callback_invalid_signature(self) -> None:
        """handle_callback 签名验证失败返回空字符串。"""
        adapter = self._make_adapter()

        xml_body = "<xml><Encrypt><![CDATA[fake]]></Encrypt></xml>"
        result = await adapter.handle_callback("ts", "nonce", "bad_sig", xml_body)
        assert result == ""

    @pytest.mark.asyncio
    async def test_handle_verify_url(self) -> None:
        """handle_verify_url 正确验证回调 URL。"""
        adapter = self._make_adapter()

        echo_str = "verify_echo_12345"

        # 加密 echostr
        random_bytes = os.urandom(16)
        msg_bytes = echo_str.encode("utf-8")
        msg_len = struct.pack("!I", len(msg_bytes))
        corp_id_bytes = CORP_ID.encode("utf-8")
        plaintext = random_bytes + msg_len + msg_bytes + corp_id_bytes

        from cryptography.hazmat.primitives import padding as sym_padding  # noqa: PLC0415
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: PLC0415

        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()

        cipher = Cipher(algorithms.AES(_DECODED_KEY), modes.CBC(_DECODED_KEY[:16]))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        encrypted_echo = base64.b64encode(ciphertext).decode("utf-8")

        # 生成签名
        timestamp = str(int(time.time()))
        nonce = "test_nonce"
        parts = sorted([TOKEN, timestamp, nonce, encrypted_echo])
        raw_str = "".join(parts)
        signature = hashlib.sha1(raw_str.encode("utf-8")).hexdigest()

        result = await adapter.handle_verify_url(timestamp, nonce, signature, encrypted_echo)
        assert result == echo_str


# ── _extract_wecom_text 测试 ──────────────────────────────────


class TestExtractWeComText:
    """_extract_wecom_text 辅助函数测试。"""

    def test_text_type(self) -> None:
        """text 类型直接返回 content。"""
        assert _extract_wecom_text("text", "Hello", {}) == "Hello"

    def test_image_type(self) -> None:
        """image 类型返回 PicUrl。"""
        assert _extract_wecom_text("image", "", {"PicUrl": "http://img.jpg"}) == "http://img.jpg"

    def test_voice_with_recognition(self) -> None:
        """voice 类型有 Recognition 时返回识别文本。"""
        assert _extract_wecom_text("voice", "", {"Recognition": "你好"}) == "你好"

    def test_voice_without_recognition(self) -> None:
        """voice 类型无 Recognition 时返回占位符。"""
        assert _extract_wecom_text("voice", "", {}) == "[语音]"

    def test_video_type(self) -> None:
        """video 类型返回占位符。"""
        assert _extract_wecom_text("video", "", {}) == "[视频]"

    def test_location_type(self) -> None:
        """location 类型返回位置标签。"""
        assert _extract_wecom_text("location", "", {"Label": "北京"}) == "[位置] 北京"

    def test_link_type(self) -> None:
        """link 类型返回描述。"""
        assert _extract_wecom_text("link", "", {"Description": "链接描述"}) == "链接描述"

    def test_unknown_type_fallback(self) -> None:
        """未知类型降级返回 content。"""
        assert _extract_wecom_text("unknown", "fallback", {}) == "fallback"


# ── MessageNormalizer wecom 注册测试 ──────────────────────────────────


class TestMessageNormalizerWeCom:
    """MessageNormalizer 中 wecom 标准化器测试。"""

    def setup_method(self) -> None:
        """每个测试前创建 normalizer。"""
        self.normalizer = MessageNormalizer()

    def test_wecom_normalizer_registered(self) -> None:
        """wecom 标准化器已注册。"""
        assert "wecom" in self.normalizer._normalizers
        assert "wecom" in self.normalizer._denormalizers

    def test_normalize_wecom_text_message(self) -> None:
        """标准化企业微信文本消息。"""
        raw = {
            "FromUserName": "user1",
            "ToUserName": CORP_ID,
            "MsgType": "text",
            "Content": "你好",
            "MsgId": "msg123",
            "CreateTime": "1700000000",
            "AgentID": str(AGENT_ID),
        }

        result = self.normalizer.normalize("wecom", raw)

        assert isinstance(result, UnifiedMessage)
        assert result.channel_type == "wecom"
        assert result.channel_user_id == "user1"
        assert result.unified_user_id == "wecom:user1"
        assert result.content == "你好"
        assert result.content_type == "text"
        assert result.message_id == "msg123"

    def test_normalize_wecom_image_message(self) -> None:
        """标准化企业微信图片消息。"""
        raw = {
            "FromUserName": "user1",
            "MsgType": "image",
            "Content": "",
            "PicUrl": "http://example.com/img.jpg",
            "MsgId": "img123",
            "CreateTime": "1700000000",
        }

        result = self.normalizer.normalize("wecom", raw)
        assert result.content == "http://example.com/img.jpg"
        assert result.content_type == "image"

    def test_normalize_wecom_unknown_user(self) -> None:
        """无 FromUserName 时 unified_user_id 为 wecom:unknown。"""
        raw = {
            "FromUserName": "",
            "MsgType": "text",
            "Content": "hello",
            "MsgId": "123",
            "CreateTime": "0",
        }

        result = self.normalizer.normalize("wecom", raw)
        assert result.unified_user_id == "wecom:unknown"

    def test_denormalize_wecom_text(self) -> None:
        """反标准化文本响应。"""
        response = UnifiedResponse(
            message_id="msg1",
            channel_type="wecom",
            content="Hello WeCom",
            content_type="text",
        )

        result = self.normalizer.denormalize("wecom", response)
        assert result["msgtype"] == "text"
        assert result["text"]["content"] == "Hello WeCom"

    def test_denormalize_wecom_card_to_markdown(self) -> None:
        """卡片响应降级为 markdown。"""
        response = UnifiedResponse(
            message_id="msg1",
            channel_type="wecom",
            content="Card content",
            content_type="card",
            card_config={"header": {"title": {"content": "Card Title"}}},
        )

        result = self.normalizer.denormalize("wecom", response)
        assert result["msgtype"] == "markdown"
        assert "Card Title" in result["markdown"]["content"]

    def test_normalize_unsupported_channel_raises(self) -> None:
        """不支持的渠道类型抛出 ValueError。"""
        with pytest.raises(ValueError, match="Unsupported channel type"):
            self.normalizer.normalize("unknown_channel", {})

    def test_denormalize_unsupported_channel_raises(self) -> None:
        """不支持的渠道类型反标准化抛出 ValueError。"""
        response = UnifiedResponse(
            message_id="msg1",
            channel_type="unknown",
            content="test",
            content_type="text",
        )
        with pytest.raises(ValueError, match="Unsupported channel type"):
            self.normalizer.denormalize("unknown_channel", response)
