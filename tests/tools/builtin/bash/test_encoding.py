"""Bash 编码模块单元测试 — EncodingHandler 自适应编码。

测试覆盖：
- UTF-8 解码（Git Bash / 现代工具）
- GBK/CP936 解码（Windows CMD 中文输出）
- 混合编码优先级（UTF-8 > 系统编码 > replace 兜底）
- 空输入边界情况
- safe_cmd_encode 命令编码
- get_system_encoding 系统编码检测
"""

import locale
import platform
from unittest.mock import patch

import pytest

from tools.builtin.bash.encoding import EncodingHandler


# ============================================================
# 测试数据（使用 hex/escape 序列避免 SyntaxError）
# ============================================================

# "你好世界" 的 GBK 编码
GBK_NI_HAO_SHI_JIE = (
    b'\xc4\xe3\xba\xc3\xca\xc0\xbd\xe7'  # 你好世界
)
# "测试" 的 GBK 编码
GBK_TEST = b'\xb2\xe2\xca\xd4'
# "驱动器" 的 GBK 编码
GBK_DRIVE = b'\xc7\xfd\xb6\xaf\xc6\xf7'
# "目录" 的 GBK 编码
GBK_DIR = b'\xc4\xbf\xc2\xbc'
# Windows CMD dir 输出模拟（GBK）
GBK_DIR_OUTPUT = (
    b' \xc7\xfd\xb6\xaf\xc6\xf7 D \xd6\xd0\xb5\xc4\xbe\xed\xca\xc7 \xca\xfd\xbe\xdd\r\n'
    b' \xbe\xed\xb5\xc4\xd0\xf2\xc1\xd0\xba\xc5\xca\xc7 1234-ABCD\r\n\r\n'
    b' D:\\\xb2\xe2\xca\xd4 \xb5\xc4\xc4\xbf\xc2\xbc\r\n\r\n'
    b'2024/01/01  10:00    <DIR>          .\r\n'
)
# UTF-8 中文测试数据
UTF8_NI_HAO_SHI_JIE = "\u4f60\u597d\u4e16\u754c".encode("utf-8")
UTF8_TEST_FOLDER = "\u6d4b\u8bd5\u6587\u4ef6\u5939".encode("utf-8")
UTF8_TEST_FILE = "\u6d4b\u8bd5\u6587\u4ef6".encode("utf-8")
# 混合内容（UTF-8）
UTF8_MIXED = "\u6587\u4ef6\u8def\u5f84: /data/\u6d4b\u8bd5/result.log".encode("utf-8")
# GBK 混合内容
GBK_MIXED = "\u6587\u4ef6\u8def\u5f84: /data/\u6d4b\u8bd5/result.log".encode("gbk")
# 纯 ASCII
ASCII_HELLO = b"Hello World"
EMPTY = b""


class TestDecodeOutputLine:
    """自适应解码测试"""

    # ---- UTF-8 解码 ----

    def test_decode_utf8_chinese(self):
        """UTF-8 编码的中文应正确解码"""
        result = EncodingHandler.decode_output_line(UTF8_NI_HAO_SHI_JIE)
        assert result == "\u4f60\u597d\u4e16\u754c"

    def test_decode_utf8_mixed_content(self):
        """UTF-8 混合内容（中英文）应正确解码"""
        result = EncodingHandler.decode_output_line(UTF8_MIXED)
        assert "\u6d4b\u8bd5" in result
        assert "result.log" in result

    def test_decode_ascii(self):
        """纯 ASCII 文本应正确解码"""
        result = EncodingHandler.decode_output_line(ASCII_HELLO)
        assert result == "Hello World"

    # ---- GBK 解码 ----

    def test_decode_gbk_chinese(self):
        """GBK 编码的中文应按系统编码正确解码"""
        with patch.object(EncodingHandler, "get_system_encoding", return_value="cp936"):
            result = EncodingHandler.decode_output_line(GBK_NI_HAO_SHI_JIE)
            assert result == "\u4f60\u597d\u4e16\u754c"

    def test_decode_gbk_mixed_content(self):
        """GBK 混合内容应正确解码"""
        with patch.object(EncodingHandler, "get_system_encoding", return_value="cp936"):
            result = EncodingHandler.decode_output_line(GBK_MIXED)
            assert "\u6d4b\u8bd5" in result
            assert "result.log" in result

    # ---- 优先级 ----

    def test_utf8_takes_priority_over_gbk(self):
        """UTF-8 编码数据应优先按 UTF-8 解码，而非降级到 GBK"""
        result = EncodingHandler.decode_output_line(UTF8_NI_HAO_SHI_JIE)
        assert result == "\u4f60\u597d\u4e16\u754c"

    def test_gbk_fallback_when_utf8_fails(self):
        """UTF-8 解码失败时，应降级到系统编码"""
        with patch.object(EncodingHandler, "get_system_encoding", return_value="cp936"):
            result = EncodingHandler.decode_output_line(GBK_NI_HAO_SHI_JIE)
            assert result == "\u4f60\u597d\u4e16\u754c"

    # ---- 边界 ----

    def test_empty_bytes(self):
        """空字节输入应返回空字符串"""
        result = EncodingHandler.decode_output_line(EMPTY)
        assert result == ""

    def test_null_byte_removal(self):
        """输出中的 null bytes 应被移除"""
        data = b"hello\x00world"
        result = EncodingHandler.decode_output_line(data)
        assert "\x00" not in result
        assert "hello" in result
        assert "world" in result

    # ---- Windows CMD 真实场景模拟 ----

    def test_windows_cmd_dir_chinese(self):
        """模拟 Windows CMD 'dir' 命令的中文输出（GBK 编码）"""
        with patch.object(EncodingHandler, "get_system_encoding", return_value="cp936"):
            result = EncodingHandler.decode_output_line(GBK_DIR_OUTPUT)
            # 验证 GBK 解码正确
            assert '\u9a71\u52a8\u5668' in result   # "驱动器"
            assert '\u6d4b\u8bd5' in result         # "测试"
            assert '\u76ee\u5f55' in result         # "目录"

    def test_git_bash_ls_chinese(self):
        """模拟 Git Bash 'ls' 命令的中文输出（UTF-8 编码）"""
        utf8_output = (
            b"total 8\n"
            b"drwxr-xr-x 1 user 197609 0 Jan  1 10:00 " + UTF8_TEST_FOLDER + b"/\n"
            b"-rw-r--r-- 1 user 197609 42 Jan  1 10:00 " + UTF8_TEST_FILE + b".txt\n"
        )
        result = EncodingHandler.decode_output_line(utf8_output)
        assert "\u6d4b\u8bd5\u6587\u4ef6\u5939" in result
        assert "\u6d4b\u8bd5\u6587\u4ef6" in result


class TestDecodeOutputText:
    """批量输出解码测试（与 decode_output_line 相同逻辑）"""

    def test_mixed_encoding_multiline(self):
        """多行 GBK 内容应正确解码"""
        # "\u7b2c\u4e00\u884c\r\n\u7b2c\u4e8c\u884c\r\n\u7b2c\u4e09\u884c" 的 GBK 编码
        gbk_multiline = (
            b'\xb5\xda\xd2\xbb\xd0\xd0\r\n'
            b'\xb5\xda\xb6\xfe\xd0\xd0\r\n'
            b'\xb5\xda\xc8\xfd\xd0\xd0'
        )
        with patch.object(EncodingHandler, "get_system_encoding", return_value="cp936"):
            result = EncodingHandler.decode_output_text(gbk_multiline)
            # 应正确解码为 Unicode 中文
            assert "\u7b2c\u4e00\u884c" in result  # 第一行
            assert "\u7b2c\u4e8c\u884c" in result  # 第二行
            assert "\u7b2c\u4e09\u884c" in result  # 第三行


class TestGetSystemEncoding:
    """系统编码检测测试"""

    def test_returns_string(self):
        """应返回有效的编码字符串"""
        enc = EncodingHandler.get_system_encoding()
        assert isinstance(enc, str)
        assert len(enc) > 0

    def test_windows_returns_cp936(self):
        """Windows 中文环境应返回 cp936 或 gbk"""
        if platform.system() == "Windows":
            enc = EncodingHandler.get_system_encoding()
            assert enc.lower() in ("cp936", "gbk", "gb2312", "gb18030")
        else:
            enc = EncodingHandler.get_system_encoding()
            assert enc.lower() in ("utf-8", "utf8")

    def test_locale_error_fallback(self):
        """locale.getpreferredencoding() 异常时应回退到 utf-8"""
        with patch("locale.getpreferredencoding", side_effect=Exception("mock error")):
            enc = EncodingHandler.get_system_encoding()
            assert enc == "utf-8"


class TestSafeCmdEncode:
    """命令安全编码测试"""

    def test_ascii_passthrough(self):
        """纯 ASCII 命令应原样返回"""
        cmd = "ls -la /tmp"
        result = EncodingHandler.safe_cmd_encode(cmd)
        assert result == cmd

    def test_encode_error_handling(self):
        """编码异常时应安全处理，不抛异常"""
        with patch("locale.getpreferredencoding", return_value="ascii"):
            cmd = 'echo "test"'
            result = EncodingHandler.safe_cmd_encode(cmd)
            assert isinstance(result, str)

    def test_empty_command(self):
        """空命令应安全处理"""
        result = EncodingHandler.safe_cmd_encode("")
        assert result == ""


class TestSurrogateescapeFallback:
    """UTF-8 + surrogateescape 中间降级测试

    模拟 WSL 通过 cmd /c 执行时，大部分输出是 UTF-8
    但混入少量无效字节的场景。
    """

    def test_mostly_utf8_with_few_invalid_bytes(self):
        """大部分是有效 UTF-8，少量无效字节 → UTF-8 + surrogateescape"""
        # "你好世界" 的 UTF-8: e4 bd a0 e5 a5 bd e4 b8 96 e7 95 8c
        # "你好" 的 UTF-8: e4 bd a0 e5 a5 bd
        # 混合有效 UTF-8 + 无效字节 0xFF（不会是有效 UTF-8 的一部分）
        valid = '\u4f60\u597d'.encode('utf-8')   # "你好" UTF-8: e4 bd a0 e5 a5 bd
        invalid = b'\xff\xfe'                      # 无效 UTF-8 字节
        mixed = valid + invalid + b' world'

        # 严格 UTF-8 应失败
        with pytest.raises(UnicodeDecodeError):
            mixed.decode('utf-8')

        # surrogateescape 应成功，保留无效字节
        result = EncodingHandler.decode_output_line(mixed)
        assert '\u4f60\u597d' in result
        assert 'world' in result

    def test_all_valid_utf8_no_surrogateescape_needed(self):
        """全部是有效 UTF-8 → 严格 UTF-8 直接通过"""
        data = '\u4f60\u597d\u4e16\u754c'.encode('utf-8')
        result = EncodingHandler.decode_output_line(data)
        assert result == '\u4f60\u597d\u4e16\u754c'

    def test_all_invalid_utf8_falls_through(self):
        """全部是无效 UTF-8 → surrogateescape 会产生很多 surrogate，降级到系统编码"""
        # 纯 GBK 字节序列（通常是无效 UTF-8）
        data = b'\xb2\xe2\xca\xd4\xce\xc4\xbc\xfe'  # "测试文件" GBK
        with patch.object(EncodingHandler, "get_system_encoding", return_value="cp936"):
            result = EncodingHandler.decode_output_line(data)
            assert '\u6d4b\u8bd5\u6587\u4ef6' in result

    def test_surrogateescape_empty_data(self):
        """空数据应直接返回空字符串"""
        result = EncodingHandler.decode_output_line(b'')
        assert result == ''


class TestWslCommandDetection:
    """WSL 命令检测和参数解析测试"""

    @classmethod
    def setup_class(cls):
        from tools.builtin.bash.process_manager import ProcessManager
        cls.PM = ProcessManager

    def test_is_wsl_command_basic(self):
        """基础 WSL 命令检测"""
        assert self.PM._is_wsl_command("wsl ls -la")
        assert self.PM._is_wsl_command("wsl echo hello")
        assert self.PM._is_wsl_command("wsl.exe ls -la")

    def test_is_wsl_command_with_flags(self):
        """带 WSL 标志的命令"""
        assert self.PM._is_wsl_command("wsl -d Ubuntu-20.04 ls")
        assert self.PM._is_wsl_command("wsl --distribution Ubuntu bash")

    def test_is_not_wsl_command(self):
        """非 WSL 命令"""
        assert not self.PM._is_wsl_command("ls -la")
        assert not self.PM._is_wsl_command("bash -c echo")
        assert not self.PM._is_wsl_command("wsl2 ls")  # wsl2 是不同命令
        assert not self.PM._is_wsl_command("")          # 空命令

    def test_parse_wsl_args_basic(self):
        """基础 WSL 参数解析（使用 -e 跳过登录 shell）"""
        args = self.PM._parse_wsl_args("wsl ls -la")
        assert args == ["wsl", "-e", "bash", "-c", "ls -la"]

    def test_parse_wsl_args_with_distribution(self):
        """带发行版标志的 WSL 参数解析"""
        args = self.PM._parse_wsl_args("wsl -d Ubuntu-20.04 ls /mnt/c")
        assert args == ["wsl", "-d", "Ubuntu-20.04", "-e", "bash", "-c", "ls /mnt/c"]

    def test_parse_wsl_args_exe(self):
        """wsl.exe 形式的参数解析"""
        args = self.PM._parse_wsl_args("wsl.exe python script.py")
        assert args == ["wsl", "-e", "bash", "-c", "python script.py"]

    def test_parse_wsl_args_empty(self):
        """只有 wsl 没有参数"""
        args = self.PM._parse_wsl_args("wsl")
        assert args == ["wsl"]

    def test_parse_wsl_args_with_spaces(self):
        """WSL 前缀有多余空格"""
        args = self.PM._parse_wsl_args("  wsl   ls -la  ")
        assert args == ["wsl", "-e", "bash", "-c", "ls -la"]

    def test_parse_wsl_args_shell_variable(self):
        """Shell 变量展开保留（$VAR 不被引号包裹）"""
        args = self.PM._parse_wsl_args("wsl VAR=hello echo $VAR")
        assert args[0] == "wsl"
        assert "-e" in args
        assert "bash" in args
        cmd = args[-1]
        assert "VAR=hello" in cmd
        assert "$VAR" in cmd
        assert "'$VAR'" not in cmd

    def test_parse_wsl_args_pipe(self):
        """管道语法保留（不被引号包裹）"""
        args = self.PM._parse_wsl_args('wsl ls -la | grep ".py"')
        assert args[0:4] == ["wsl", "-e", "bash", "-c"]
        cmd = args[4]
        assert "ls" in cmd and "|" in cmd and "grep" in cmd
        assert "'|'" not in cmd

    def test_parse_wsl_args_flags_only(self):
        """只有 WSL 标志没有命令"""
        args = self.PM._parse_wsl_args("wsl -d Ubuntu-20.04")
        assert args == ["wsl", "-d", "Ubuntu-20.04"]

    def test_parse_wsl_args_user_flag(self):
        """带 -u 用户标志"""
        args = self.PM._parse_wsl_args("wsl -u root whoami")
        assert args == ["wsl", "-u", "root", "-e", "bash", "-c", "whoami"]
