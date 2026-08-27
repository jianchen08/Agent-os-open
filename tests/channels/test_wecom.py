# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""企业微信通道测试（A5.2 渠道 per-file 100% 批）。

覆盖 channel_wecom 四个源文件：
- adapter.py：input/output 适配器 + 组合适配器（回调签名/解密/验证 URL）
- crypto.py：AES 加解密、签名验证、corp_id 校验
- helpers.py：XML 解析、消息类型文本提取
- stream_client.py：HTTP 发送/令牌刷新/回调触发

所有外部 I/O 以 mock 会话注入（与 test_dingtalk.py 同范式）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

from tests.channels.conftest import use_channel

use_channel("wecom")
from adapter import WeComAdapter, WeComInputAdapter, WeComOutputAdapter  # noqa: E402
from crypto import WecomCrypto  # noqa: E402
from helpers import _extract_encrypt, _extract_wecom_text, _parse_message_xml  # noqa: E402
from stream_client import WeComStreamClient  # noqa: E402

# ── 测试密钥（企业微信 43 字符 EncodingAESKey）───────────────────────────────
_AES_KEY_B64 = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
_TOKEN = "test_token"
_CORP_ID = "ww1234567890"


def _make_crypto() -> WecomCrypto:
    return WecomCrypto(token=_TOKEN, encoding_aes_key=_AES_KEY_B64, corp_id=_CORP_ID)


def _aes_key() -> bytes:
    return base64.b64decode(_AES_KEY_B64 + "=")


def _encrypt(plaintext: bytes) -> str:
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = _aes_key()
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    enc = cipher.encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode("utf-8")


def _encrypt_message(msg_xml: str) -> str:
    """构造企业微信格式密文：random(16) + msg_len(4) + msg + corp_id。"""
    payload = (
        b"0123456789abcdef"
        + struct.pack("!I", len(msg_xml.encode()))
        + msg_xml.encode()
        + _CORP_ID.encode()
    )
    return _encrypt(payload)


def _signature(timestamp: str, nonce: str, msg_encrypt: str) -> str:
    parts = sorted([_TOKEN, timestamp, nonce, msg_encrypt])
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════
# WeComCrypto 加解密
# ═══════════════════════════════════════════════════════════


class TestWecomCrypto:
    def test_init_config_wiring_via_behavior(self) -> None:
        """构造四要素（token/aes_key/iv/corp_id）全部经行为生效：
        token 被签名校验消费、aes_key(IV=key[:16]) 被解密消费、corp_id 被 CorpID 校验消费。"""
        c = _make_encrypto()
        msg_xml = "<xml><Content>wiring</Content></xml>"
        enc = _encrypt_message(msg_xml)
        ts, nonce = "1700000000", "abc wiring"
        # token：用同一 token 计算的签名通过，篡改即失败
        assert c.verify_signature(ts, nonce, enc, _signature(ts, nonce, enc)) is True
        assert c.verify_signature(ts, nonce, enc, "bad") is False
        # aes_key/iv/corp_id：外部按 AES-CBC(key[:16]) + corp 尾部拼装加密的报文可完整解出
        assert c.decrypt_message(f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>") == msg_xml

    def test_verify_signature_ok_and_mismatch(self) -> None:
        c = _make_encrypto()
        enc = _encrypt_message("<xml><Content>hi</Content></xml>")
        ts, nonce = "1700000000", "abc123"
        good = _signature(ts, nonce, enc)
        assert c.verify_signature(ts, nonce, enc, good) is True
        assert c.verify_signature(ts, nonce, enc, "bad") is False

    def test_decrypt_message_roundtrip(self) -> None:
        c = _make_encrypto()
        msg_xml = "<xml><Content>你好</Content></xml>"
        enc_xml = f"<xml><Encrypt><![CDATA[{_encrypt_message(msg_xml)}]]></Encrypt></xml>"
        assert c.decrypt_message(enc_xml) == msg_xml

    def test_decrypt_message_corp_id_mismatch_raises(self) -> None:
        c = _make_encrypto()
        msg = (
            b"0123456789abcdef"
            + struct.pack("!I", 3)
            + b"abc"
            + b"wrong_corp_id_other"
        )
        enc_xml = f"<xml><Encrypt><![CDATA[{_encrypt(msg)}]]></Encrypt></xml>"
        with pytest.raises(ValueError, match="CorpID mismatch"):
            c.decrypt_message(enc_xml)

    def test_decrypt_echo(self) -> None:
        c = _make_encrypto()
        echo = b"0123456789abcdef" + struct.pack("!I", 5) + b"hello" + _CORP_ID.encode()
        assert c.decrypt_echo(_encrypt(echo)) == "hello"

    def test_encrypt_response_roundtrip(self) -> None:
        c = _make_encrypto()
        reply = "<xml><Content>ok</Content></xml>"
        encrypted_xml = c.encrypt_response(reply)
        # 含 Encrypt/MsgSignature/TimeStamp/Nonce 四元素
        assert "<Encrypt>" in encrypted_xml and "<MsgSignature>" in encrypted_xml
        # 用 decrypt_message 验证可还原（encrypt_response 内部附 corp_id）
        assert c.decrypt_message(encrypted_xml) == reply

    def test_encrypt_response_signature_valid(self) -> None:
        import re

        c = _make_encrypto()
        encrypted_xml = c.encrypt_response("<xml>reply</xml>")
        ts = re.search(r"<TimeStamp>(\d+)</TimeStamp>", encrypted_xml)
        nonce = re.search(r"<Nonce><!\[CDATA\[(.+?)\]\]></Nonce>", encrypted_xml)
        sig = re.search(r"<MsgSignature><!\[CDATA\[(.+?)\]\]></MsgSignature>", encrypted_xml)
        enc = re.search(r"<Encrypt><!\[CDATA\[(.+?)\]\]></Encrypt>", encrypted_xml)
        assert ts and nonce and sig and enc
        assert _signature(ts.group(1), nonce.group(1), enc.group(1)) == sig.group(1)

    def test_extract_encrypt_missing_and_invalid(self) -> None:
        assert _extract_encrypt("<xml><Other>x</Other></xml>") == ""
        assert _extract_encrypt("not xml at all") == ""

    def test_aes_decrypt_bad_ciphertext_raises(self) -> None:
        c = _make_encrypto()
        with pytest.raises(ValueError, match="AES decrypt failed"):
            c._aes_decrypt("!!!not-base64-encrypted!!!")

    def test_aes_decrypt_bad_padding_raises(self) -> None:
        """密文 base64 合法但 PKCS7 填充非法 → ValueError（解密失败路径）。"""
        c = _make_encrypto()
        # 用 16 字节全零（IV 对齐）密文，解密后填充校验必失败
        bogus = base64.b64encode(b"\x00" * 16).decode("ascii")
        with pytest.raises(ValueError, match="AES decrypt failed"):
            c._aes_decrypt(bogus)

    def test_decrypt_message_struct_error_wrapped(self) -> None:
        """密文可解但长度不足 20 字节 → struct.error 包装为 ValueError（回调契约）。"""
        c = _make_encrypto()
        enc = _encrypt(b"short")
        enc_xml = f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
        with pytest.raises(ValueError, match="Invalid encrypted message"):
            c.decrypt_message(enc_xml)

    def test_extract_encrypt_missing_encrypt_field_raises(self) -> None:
        """_extract_encrypt 缺失 Encrypt 节点 → ValueError（decrypt_message 契约）。"""
        c = _make_encrypto()
        with pytest.raises(ValueError, match="Missing Encrypt field"):
            c._extract_encrypt("<xml><Other>x</Other></xml>")
        with pytest.raises(ValueError, match="Invalid XML"):
            c._extract_encrypt("<<<broken")


def _make_encrypto() -> WecomCrypto:
    return WecomCrypto(token=_TOKEN, encoding_aes_key=_AES_KEY_B64, corp_id=_CORP_ID)


# ═══════════════════════════════════════════════════════════
# helpers：XML 解析与文本提取
# ═══════════════════════════════════════════════════════════


class TestWeComHelpers:
    def test_parse_message_xml(self) -> None:
        msg = _parse_message_xml("<xml><Content>你好</Content><MsgType>text</MsgType></xml>")
        assert msg == {"Content": "你好", "MsgType": "text"}

    def test_parse_message_xml_invalid_returns_empty(self) -> None:
        assert _parse_message_xml("<<<broken") == {}

    def test_extract_wecom_text_variants(self) -> None:
        assert _extract_wecom_text("text", "hi", {}) == "hi"
        # image 分支实现：PicUrl 存在返回 URL，缺失返回默认 "[图片]"
        assert _extract_wecom_text("image", "", {"PicUrl": "http://x/y.png"}) == "http://x/y.png"
        assert _extract_wecom_text("image", "", {}) == "[图片]"
        assert _extract_wecom_text("voice", "", {"Recognition": "识别文本"}) == "识别文本"
        assert _extract_wecom_text("voice", "", {}) == "[语音]"
        assert _extract_wecom_text("video", "", {}) == "[视频]"
        assert _extract_wecom_text("shortvideo", "", {}) == "[视频]"
        assert _extract_wecom_text("location", "", {"Label": "公司"}) == "[位置] 公司"
        assert _extract_wecom_text("location", "", {}) == "[位置]"
        assert _extract_wecom_text("link", "desc", {"Description": "描述"}) == "描述"
        assert _extract_wecom_text("link", "desc", {}) == "desc"
        # 未知类型 → 内容或整个消息兜底
        assert _extract_wecom_text("unknown", "x", {}) == "x"
        assert _extract_wecom_text("unknown", "", {"k": "v"}) == str({"k": "v"})


# ═══════════════════════════════════════════════════════════
# WeComInputAdapter
# ═══════════════════════════════════════════════════════════


class TestWeComInputAdapter:
    def test_raw_to_state(self) -> None:
        state = WeComInputAdapter._raw_to_state(
            {
                "FromUserName": "u1",
                "ToUserName": "bot",
                "MsgType": "text",
                "Content": "hello",
                "MsgId": "msg-1",
                "AgentID": "1000002",
            }
        )
        assert state["user_input"] == "hello"
        assert state["_channel_type"] == "wecom"
        assert state["_channel_user_id"] == "u1"
        assert state["_to_user"] == "bot"
        assert state["_agent_id"] == "1000002"
        assert state["iteration"] == 1
        assert state["should_stop"] is False

    def test_raw_to_state_defaults(self) -> None:
        state = WeComInputAdapter._raw_to_state({})
        assert state["user_input"] == ""
        assert state["_channel_user_id"] == ""
        assert state["_agent_id"] == ""
        assert state["should_stop"] is False

    @pytest.mark.asyncio
    async def test_enqueue_and_receive(self) -> None:
        adapter = WeComInputAdapter()
        raw = {"MsgType": "text", "Content": "queue-msg", "FromUserName": "u"}
        await adapter.enqueue_message(raw)
        state = await adapter.receive()
        assert state["user_input"] == "queue-msg"
        assert state["_channel_user_id"] == "u"


# ═══════════════════════════════════════════════════════════
# WeComOutputAdapter
# ═══════════════════════════════════════════════════════════


class TestWeComOutputAdapter:
    def _client(self) -> MagicMock:
        client = MagicMock(spec=WeComStreamClient)
        client.send_message = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_send_normal_result(self) -> None:
        client = self._client()
        out = WeComOutputAdapter(client)
        out.set_channel_user_id("u1")
        await out.send({"raw_result": "done", "_channel_user_id": "u1"})
        client.send_message.assert_awaited_once_with("u1", "done")

    @pytest.mark.asyncio
    async def test_send_error_prefix(self) -> None:
        client = self._client()
        out = WeComOutputAdapter(client)
        await out.send({"_channel_user_id": "u1", "raw_error": "boom"})
        client.send_message.assert_awaited_once_with("u1", "❌ 错误: boom")

    @pytest.mark.asyncio
    async def test_send_no_user_id_skips(self) -> None:
        client = self._client()
        out = WeComOutputAdapter(client)
        await out.send({"raw_result": "x"})
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_stream_accumulates_and_flushes(self) -> None:
        client = self._client()
        out = WeComOutputAdapter(client)
        out.set_channel_user_id("u9")
        await out.send_stream({"text": "Hel"})
        await out.send_stream({"text": "lo", "flush": True})
        client.send_message.assert_awaited_once_with("u9", "Hello")
        # flush 后累积清空，end 再发为空
        await out.send_stream({"text": "", "type": "end"})
        client.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_propagates_send_failure(self) -> None:
        """底层发送失败 → 异常传播给调用方（适配器不吞错，管道可感知丢消息）。"""
        client = self._client()
        client.send_message = AsyncMock(side_effect=RuntimeError("WeCom send message failed: errcode=40016"))
        out = WeComOutputAdapter(client)
        with pytest.raises(RuntimeError, match="WeCom send message failed"):
            await out.send({"raw_result": "hello", "_channel_user_id": "u1"})

    @pytest.mark.asyncio
    async def test_send_stream_flush_propagates_send_failure(self) -> None:
        """流式 flush 发送失败 → 异常传播，不静默丢弃累积文本。"""
        client = self._client()
        client.send_message = AsyncMock(side_effect=RuntimeError("WeCom send message failed"))
        out = WeComOutputAdapter(client)
        out.set_channel_user_id("u9")
        with pytest.raises(RuntimeError, match="WeCom send message failed"):
            await out.send_stream({"text": "完整", "flush": True})


# ═══════════════════════════════════════════════════════════
# WeComAdapter 组合
# ═══════════════════════════════════════════════════════════


class TestWeComAdapter:
    def test_initialization_wires_callback(self) -> None:
        adapter = WeComAdapter(
            corp_id=_CORP_ID,
            agent_id=1000001,
            secret="sec",
            token=_TOKEN,
            encoding_aes_key=_AES_KEY_B64,
        )
        assert adapter.channel_type == "wecom"
        assert adapter.input_adapter is not None
        assert adapter.output_adapter is not None
        # stream_client 回调已绑定到 input_adapter.enqueue_message
        assert adapter.stream_client.on_message == adapter.input_adapter.enqueue_message

    @pytest.mark.asyncio
    async def test_start_stop_delegate(self) -> None:
        adapter = WeComAdapter(
            corp_id=_CORP_ID, agent_id=1, secret="s", token=_TOKEN, encoding_aes_key=_AES_KEY_B64
        )
        adapter.stream_client.connect = AsyncMock()
        adapter.stream_client.disconnect = AsyncMock()
        await adapter.start()
        adapter.stream_client.connect.assert_awaited_once()
        await adapter.stop()
        adapter.stream_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handle_callback_success_path(self) -> None:
        adapter = WeComAdapter(
            corp_id=_CORP_ID, agent_id=1, secret="s", token=_TOKEN, encoding_aes_key=_AES_KEY_B64
        )
        msg_xml = "<xml><Content>回调消息</Content></xml>"
        enc = _encrypt_message(msg_xml)
        body = f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
        ts, nonce = "1700000000", "n1"
        sig = _signature(ts, nonce, enc)

        received: list[dict] = []
        adapter.stream_client.on_message = AsyncMock(side_effect=lambda m: received.append(m))
        result = await adapter.handle_callback(ts, nonce, sig, body)
        assert result == msg_xml
        assert received and received[0] == {"Content": "回调消息"}

    @pytest.mark.asyncio
    async def test_handle_callback_bad_signature_returns_empty(self) -> None:
        adapter = WeComAdapter(
            corp_id=_CORP_ID, agent_id=1, secret="s", token=_TOKEN, encoding_aes_key=_AES_KEY_B64
        )
        result = await adapter.handle_callback("1", "2", "wrong-sig", "<xml/>")
        assert result == ""

    @pytest.mark.asyncio
    async def test_handle_callback_unparseable_xml_returns_raw(self) -> None:
        """解密成功但 XML 解析失败 → 返回解密的原始 XML（验证 URL 场景）。"""
        adapter = WeComAdapter(
            corp_id=_CORP_ID, agent_id=1, secret="s", token=_TOKEN, encoding_aes_key=_AES_KEY_B64
        )
        # 构造合法加密但非 XML 的明文（解密成功、_parse_message_xml 返回空）
        raw_text = "echostr-plain"
        payload = (
            b"0123456789abcdef"
            + struct.pack("!I", len(raw_text.encode()))
            + raw_text.encode()
            + _CORP_ID.encode()
        )
        enc = _encrypt(payload)
        body = f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>"
        ts, nonce = "1700000000", "n4"
        sig = _signature(ts, nonce, enc)
        assert await adapter.handle_callback(ts, nonce, sig, body) == raw_text

    @pytest.mark.asyncio
    async def test_handle_callback_decrypt_failure_returns_empty(self) -> None:
        adapter = WeComAdapter(
            corp_id=_CORP_ID, agent_id=1, secret="s", token=_TOKEN, encoding_aes_key=_AES_KEY_B64
        )
        bad_enc = _encrypt(b"short")
        body = f"<xml><Encrypt><![CDATA[{bad_enc}]]></Encrypt></xml>"
        ts, nonce = "1700000000", "n2"
        sig = _signature(ts, nonce, bad_enc)
        # 密文过短 → struct.error（ValueError 子类）→ handle_callback 捕获返回空串
        assert await adapter.handle_callback(ts, nonce, sig, body) == ""

    @pytest.mark.asyncio
    async def test_handle_verify_url(self) -> None:
        adapter = WeComAdapter(
            corp_id=_CORP_ID, agent_id=1, secret="s", token=_TOKEN, encoding_aes_key=_AES_KEY_B64
        )
        echo = b"0123456789abcdef" + struct.pack("!I", 5) + b"echo!" + _CORP_ID.encode()
        enc_echo = _encrypt(echo)
        ts, nonce = "1700000000", "n3"
        assert await adapter.handle_verify_url(ts, nonce, _signature(ts, nonce, enc_echo), enc_echo) == "echo!"

    @pytest.mark.asyncio
    async def test_handle_verify_url_bad_signature(self) -> None:
        adapter = WeComAdapter(
            corp_id=_CORP_ID, agent_id=1, secret="s", token=_TOKEN, encoding_aes_key=_AES_KEY_B64
        )
        assert await adapter.handle_verify_url("1", "2", "bad", "enc") == ""


# ═══════════════════════════════════════════════════════════
# WeComStreamClient
# ═══════════════════════════════════════════════════════════


class TestWeComStreamClient:
    def test_init_and_is_connected(self) -> None:
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_build_send_body_text_and_markdown(self) -> None:
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=42, secret="s")
        text_body = client._build_send_body("u1", "hi", "text")
        assert text_body["msgtype"] == "text"
        assert text_body["agentid"] == 42
        assert text_body["text"] == {"content": "hi"}
        md_body = client._build_send_body("u1", "**hi**", "markdown")
        assert md_body["msgtype"] == "markdown"
        assert md_body["markdown"] == {"content": "**hi**"}

    @pytest.mark.asyncio
    async def test_send_message_success(self) -> None:
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        client._ensure_token = AsyncMock()
        client._access_token = "tok"
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"errcode": 0})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False
        client._session = mock_session

        result = await client.send_message("u1", "hi")
        assert result == {"errcode": 0}
        url = mock_session.post.call_args[0][0]
        assert "/cgi-bin/message/send" in url and "access_token=tok" in url
        body = mock_session.post.call_args[1]["json"]
        assert body["touser"] == "u1"

    @pytest.mark.asyncio
    async def test_send_message_no_session_raises(self) -> None:
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        client._ensure_token = AsyncMock()
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_message("u1", "hi")

    @pytest.mark.asyncio
    async def test_send_message_api_error_raises_and_logs(self) -> None:
        """API errcode 非 0 → RuntimeError 上抛（契约：发送失败可感知，不得静默返回）。"""
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        client._ensure_token = AsyncMock()
        client._access_token = "tok"
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"errcode": 40016, "errmsg": "bad"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False
        client._session = mock_session

        with pytest.raises(RuntimeError, match="errcode=40016"):
            await client.send_message("u1", "hi")

    @pytest.mark.asyncio
    async def test_send_message_success_errcode_zero_returns_result(self) -> None:
        """成功 errcode=0 正常返回结果（行为不变量：失败上抛不改成功路径）。"""
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        client._ensure_token = AsyncMock()
        client._access_token = "tok"
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"errcode": 0})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False
        client._session = mock_session

        result = await client.send_message("u1", "hi")
        assert result == {"errcode": 0}

    @staticmethod
    def _resp(payload: dict) -> AsyncMock:
        resp = AsyncMock()
        resp.json = AsyncMock(return_value=payload)
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    @staticmethod
    def _routing_get_session(token_payloads: list[dict]) -> MagicMock:
        """按端点路由的假会话：gettoken 依次消费 token_payloads，其余端点回成功。"""
        queue = iter(token_payloads)
        session = MagicMock()
        session.closed = False

        def _get(url: str, **_kw):
            if "gettoken" in url:
                return TestWeComStreamClient._resp(next(queue))
            return TestWeComStreamClient._resp({"errcode": 0})

        session.get = MagicMock(side_effect=_get)
        # 发送走 POST（/cgi-bin/message/send），统一回成功
        session.post = MagicMock(return_value=TestWeComStreamClient._resp({"errcode": 0}))
        return session

    @pytest.mark.asyncio
    async def test_send_message_fetches_and_caches_token(self, monkeypatch) -> None:
        """首次发送取 token（corpid/secret 参与请求），未过期时第二次发送不再取。"""
        now = [1000.0]
        monkeypatch.setattr("stream_client.time.time", lambda: now[0])
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        session = self._routing_get_session([{"errcode": 0, "access_token": "tok-1", "expires_in": 7200}])
        client._session = session

        result = await client.send_message("u1", "hi")
        assert result == {"errcode": 0}
        await client.send_message("u2", "hi again")

        token_urls = [c[0][0] for c in session.get.call_args_list if "gettoken" in c[0][0]]
        assert len(token_urls) == 1  # 取 token 仅一次
        assert f"corpid={_CORP_ID}" in token_urls[0]
        send_urls = [c[0][0] for c in session.post.call_args_list]
        assert len(send_urls) == 2
        assert all(u.endswith("access_token=tok-1") for u in send_urls)

    @pytest.mark.asyncio
    async def test_send_message_refreshes_expiring_token(self, monkeypatch) -> None:
        """expire 推进到过期后，再次发送会重新取 token 且 URL 携带新 access_token。"""
        now = [1000.0]
        monkeypatch.setattr("stream_client.time.time", lambda: now[0])
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        session = self._routing_get_session(
            [
                {"errcode": 0, "access_token": "tok-1", "expires_in": 7200},
                {"errcode": 0, "access_token": "tok-2", "expires_in": 7200},
            ]
        )
        client._session = session
        await client.send_message("u1", "hi")

        now[0] += 7200  # 推进到 token 过期
        await client.send_message("u1", "hi again")

        token_urls = [c[0][0] for c in session.get.call_args_list if "gettoken" in c[0][0]]
        assert len(token_urls) == 2  # 首次 + 过期后重取
        send_urls = [c[0][0] for c in session.post.call_args_list]
        assert len(send_urls) == 2
        assert send_urls[0].endswith("access_token=tok-1")
        assert send_urls[1].endswith("access_token=tok-2")

    @pytest.mark.asyncio
    async def test_ensure_token_error_raises(self) -> None:
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        client._session = MagicMock()
        client._session.get = MagicMock(
            return_value=AsyncMock(
                __aenter__=AsyncMock(
                    return_value=AsyncMock(json=AsyncMock(return_value={"errcode": 40013, "errmsg": "invalid"}))
                ),
                __aexit__=AsyncMock(return_value=False),
            )
        )
        with pytest.raises(RuntimeError, match="WeCom get token failed"):
            await client._ensure_token()

    @pytest.mark.asyncio
    async def test_ensure_token_no_session_raises(self) -> None:
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client._ensure_token()

    @pytest.mark.asyncio
    async def test_connect_and_disconnect(self) -> None:
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        client._ensure_token = AsyncMock()
        client._session = MagicMock()
        client._session.closed = False
        await client.connect()
        assert client.is_connected
        await client.disconnect()
        # 会话已释放：再次发送报"未初始化"而非静默成功
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_message("u1", "hi")

    @pytest.mark.asyncio
    async def test_trigger_on_message_with_and_without_callback(self) -> None:
        client = WeComStreamClient(corp_id=_CORP_ID, agent_id=1, secret="s")
        got = []
        async def cb(m): got.append(m)
        client.on_message = cb
        await client.trigger_on_message({"a": 1})
        assert got == [{"a": 1}]
        client.on_message = None
        await client.trigger_on_message({"a": 1})  # 不抛
