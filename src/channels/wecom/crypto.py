"""企业微信消息加解密模块。

实现企业微信回调消息的签名验证、消息解密和回复加密。
使用 WXBizMsgCrypt 算法（AES-256-CBC），不依赖官方 SDK。

企业微信回调协议参考：
https://developer.work.weixin.qq.com/document/path/90930

核心能力：
- verify_signature: 验证回调 URL 签名
- decrypt_message: 解密回调消息 XML
- encrypt_response: 加密回复消息
"""

from __future__ import annotations

import base64
import hashlib
import logging
import struct
import time
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)


class WecomCrypto:
    """企业微信消息加解密处理器。

    使用 AES-256-CBC 算法，基于 EncodingAESKey 和 Token 进行
    消息的加解密和签名验证。

    Attributes:
        _token: 企业微信回调配置的 Token
        _aes_key: 解码后的 AES 密钥（32 字节）
        _corp_id: 企业微信 CorpID
        _iv: AES 初始向量（固定为 aes_key 的前 16 字节）

    Example::

        crypto = WecomCrypto(
            token="your_token",
            encoding_aes_key="your_encoding_aes_key",
            corp_id="your_corp_id",
        )
        # 验证回调 URL
        echo_str = crypto.verify_signature(timestamp, nonce, msg_encrypt, signature)
        # 解密消息
        xml_content = crypto.decrypt_message(encrypted_xml)
        # 加密回复
        encrypted = crypto.encrypt_response(reply_content)
    """

    def __init__(
        self,
        token: str,
        encoding_aes_key: str,
        corp_id: str,
    ) -> None:
        """初始化企业微信加解密处理器。

        Args:
            token: 企业微信回调配置的 Token
            encoding_aes_key: 企业微信回调配置的 EncodingAESKey（43 个字符）
            corp_id: 企业微信 CorpID
        """
        self._token = token
        self._corp_id = corp_id

        # EncodingAESKey 是 Base64 编码的 AES 密钥（43 字符 → 补 '=' 后 Base64 解码得到 32 字节）
        decoded = base64.b64decode(encoding_aes_key + "=")
        self._aes_key = decoded
        self._iv = decoded[:16]

    def verify_signature(
        self,
        timestamp: str,
        nonce: str,
        msg_encrypt: str,
        signature: str,
    ) -> bool:
        """验证回调消息签名。

        将 token、timestamp、nonce、msg_encrypt 排序后拼接，
        计算 SHA1 哈希并与 signature 比对。

        Args:
            timestamp: 回调请求的时间戳
            nonce: 回调请求的随机字符串
            msg_encrypt: 加密的消息体
            signature: 待验证的签名

        Returns:
            签名是否有效
        """
        calculated = self._calculate_signature(timestamp, nonce, msg_encrypt)
        if calculated != signature:
            logger.warning(
                "Signature mismatch: calculated=%s, expected=%s",
                calculated,
                signature,
            )
            return False
        return True

    def decrypt_message(self, encrypted_xml: str) -> str:
        """解密回调消息 XML。

        从加密的 XML 中提取 Encrypt 字段，解密后还原为原始消息 XML。

        Args:
            encrypted_xml: 加密的消息 XML 字符串

        Returns:
            解密后的原始消息 XML 字符串

        Raises:
            ValueError: 签名验证失败或消息格式错误
        """
        # 解析加密 XML，提取 Encrypt 字段
        encrypt_content = self._extract_encrypt(encrypted_xml)

        # AES 解密
        decrypted = self._aes_decrypt(encrypt_content)

        # 解析解密后的内容：random(16) + msg_len(4) + msg + corp_id
        # 前 16 字节为随机字符串
        msg_len = struct.unpack("!I", decrypted[16:20])[0]
        msg = decrypted[20 : 20 + msg_len]
        received_corp_id = decrypted[20 + msg_len :]

        # 验证 corp_id
        if received_corp_id.decode("utf-8") != self._corp_id:
            raise ValueError(
                f"CorpID mismatch: expected={self._corp_id}, got={received_corp_id.decode('utf-8', errors='replace')}"
            )

        return msg.decode("utf-8")

    def decrypt_echo(self, encrypted_xml: str) -> str:
        """解密验证回调 URL 时的 echostr。

        验证回调 URL 时，企业微信发送 GET 请求，echostr 是加密的，
        需要解密后返回明文。

        Args:
            encrypted_xml: 加密的 echostr 字符串

        Returns:
            解密后的明文 echostr
        """
        decrypted = self._aes_decrypt(encrypted_xml)
        # 解析：random(16) + msg_len(4) + msg + corp_id
        msg_len = struct.unpack("!I", decrypted[16:20])[0]
        msg = decrypted[20 : 20 + msg_len]
        return msg.decode("utf-8")

    def encrypt_response(self, reply_msg: str) -> str:
        """加密回复消息。

        将明文回复消息加密，生成可返回给企业微信的加密 XML。

        Args:
            reply_msg: 明文回复消息 XML 字符串

        Returns:
            加密后的完整回复 XML 字符串
        """
        import os  # noqa: PLC0415

        # 构造明文：random(16) + msg_len(4) + msg + corp_id
        random_bytes = os.urandom(16)
        msg_bytes = reply_msg.encode("utf-8")
        msg_len = struct.pack("!I", len(msg_bytes))
        corp_id_bytes = self._corp_id.encode("utf-8")
        plaintext = random_bytes + msg_len + msg_bytes + corp_id_bytes

        # AES 加密
        encrypted = self._aes_encrypt(plaintext)

        # 生成签名
        timestamp = str(int(time.time()))
        nonce = hashlib.md5(os.urandom(16)).hexdigest()[:10]
        signature = self._calculate_signature(timestamp, nonce, encrypted)

        # 构造返回 XML
        return (
            f"<xml>"
            f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
            f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
            f"<TimeStamp>{timestamp}</TimeStamp>"
            f"<Nonce><![CDATA[{nonce}]]></Nonce>"
            f"</xml>"
        )

    # ── 内部方法 ──────────────────────────────────────

    def _calculate_signature(
        self,
        timestamp: str,
        nonce: str,
        msg_encrypt: str,
    ) -> str:
        """计算消息签名。

        将 token、timestamp、nonce、msg_encrypt 按字典序排序后拼接，
        计算 SHA1 哈希。

        Args:
            timestamp: 时间戳
            nonce: 随机字符串
            msg_encrypt: 加密消息

        Returns:
            SHA1 签名字符串
        """
        parts = sorted([self._token, timestamp, nonce, msg_encrypt])
        raw = "".join(parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _extract_encrypt(self, xml_str: str) -> str:
        """从加密 XML 中提取 Encrypt 字段。

        Args:
            xml_str: 加密消息 XML

        Returns:
            Encrypt 字段内容

        Raises:
            ValueError: XML 格式错误或缺少 Encrypt 字段
        """
        try:
            root = ET.fromstring(xml_str)
            encrypt_node = root.find("Encrypt")
            if encrypt_node is None or encrypt_node.text is None:
                raise ValueError("Missing Encrypt field in XML")
            return encrypt_node.text
        except ET.ParseError as exc:
            raise ValueError(f"Invalid XML: {exc}") from exc

    def _aes_decrypt(self, encrypted_text: str) -> bytes:
        """AES-256-CBC 解密。

        Args:
            encrypted_text: Base64 编码的密文

        Returns:
            解密后的字节串

        Raises:
            ValueError: 解密失败
        """
        try:
            ciphertext = base64.b64decode(encrypted_text)
            cipher = Cipher(algorithms.AES(self._aes_key), modes.CBC(self._iv))
            decryptor = cipher.decryptor()
            padded = decryptor.update(ciphertext) + decryptor.finalize()

            # 移除 PKCS7 填充
            unpadder = sym_padding.PKCS7(128).unpadder()
            data = unpadder.update(padded) + unpadder.finalize()
            return data
        except Exception as exc:
            raise ValueError(f"AES decrypt failed: {exc}") from exc

    def _aes_encrypt(self, plaintext: bytes) -> str:
        """AES-256-CBC 加密。

        Args:
            plaintext: 明文字节串

        Returns:
            Base64 编码的密文
        """
        # PKCS7 填充
        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()

        cipher = Cipher(algorithms.AES(self._aes_key), modes.CBC(self._iv))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()

        return base64.b64encode(ciphertext).decode("utf-8")
