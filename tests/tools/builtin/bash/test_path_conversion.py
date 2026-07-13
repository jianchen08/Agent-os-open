"""Bash 工具 Windows 路径转 WSL 路径单元测试。

测试覆盖：
- 盘符路径转换（反斜杠 / 正斜杠）
- Windows 长路径前缀转换
- \\wsl$ 和 \\wsl.localhost UNC 路径转换
- 带空格、括号路径的引号保留
- 多路径同时转换
- 不转换相对路径、环境变量、网络共享、Unix 路径
- 非 Windows 平台原样返回
- 空命令边界情况
"""

from __future__ import annotations

from unittest.mock import patch

from tools.builtin.bash.process_manager import ProcessManager


class TestConvertWindowsPathsForWsl:
    """Windows 路径转 WSL 路径测试。"""

    # ---- 盘符路径 ----

    def test_drive_backslash(self):
        """反斜杠盘符路径应正确转换。"""
        assert ProcessManager._convert_windows_paths_for_wsl(r"cat D:\data\file.txt") == "cat /mnt/d/data/file.txt"

    def test_drive_forward_slash(self):
        """正斜杠盘符路径应正确转换。"""
        assert ProcessManager._convert_windows_paths_for_wsl("cat D:/data/file.txt") == "cat /mnt/d/data/file.txt"

    def test_drive_uppercase(self):
        """大写盘符应转小写。"""
        assert ProcessManager._convert_windows_paths_for_wsl(r"ls C:\Users") == "ls /mnt/c/Users"

    def test_drive_root_only(self):
        """仅盘符根目录应正确转换。"""
        assert ProcessManager._convert_windows_paths_for_wsl("dir D:\\") == "dir /mnt/d"

    # ---- 长路径前缀 ----

    def test_long_path_prefix(self):
        """Windows 长路径前缀 \\\\?\\ 应被剥离。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl(r"cat \\?\D:\very\long\path\file.txt")
            == "cat /mnt/d/very/long/path/file.txt"
        )

    # ---- WSL UNC 路径 ----

    def test_wsl_unc_path(self):
        """\\wsl$ 路径应转换为 WSL 内部路径。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl(r"cat \\wsl$\Ubuntu\home\user\file.txt")
            == "cat /home/user/file.txt"
        )

    def test_wsl_localhost_unc_path(self):
        """\\wsl.localhost 路径应转换为 WSL 内部路径。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl(r"cat \\wsl.localhost\Ubuntu\home\user\file.txt")
            == "cat /home/user/file.txt"
        )

    # ---- 引号与特殊字符 ----

    def test_quoted_path_with_spaces(self):
        """带引号的含空格路径应保留引号。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl(r'cat "D:\path with space\file.txt"')
            == 'cat "/mnt/d/path with space/file.txt"'
        )

    def test_quoted_path_with_parentheses(self):
        """带引号的含括号路径（如 Program Files (x86)）应正确转换。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl(r'cat "D:\Program Files (x86)\app.exe"')
            == 'cat "/mnt/d/Program Files (x86)/app.exe"'
        )

    def test_single_quoted_path(self):
        """单引号包裹路径应保留单引号。"""
        assert ProcessManager._convert_windows_paths_for_wsl(r"cat 'D:\data\file.txt'") == "cat '/mnt/d/data/file.txt'"

    # ---- 多路径与命令混合 ----

    def test_multiple_paths(self):
        """一条命令中多个 Windows 路径都应转换。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl(r"cp D:\src\a.txt E:\dst\b.txt")
            == "cp /mnt/d/src/a.txt /mnt/e/dst/b.txt"
        )

    def test_mixed_with_unix_path(self):
        """Windows 路径和 Unix 路径混合时只转换 Windows 路径。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl(r"cp D:\src\a.txt /home/user/b.txt")
            == "cp /mnt/d/src/a.txt /home/user/b.txt"
        )

    # ---- 不应转换的情况 ----

    def test_relative_path_not_converted(self):
        """相对路径不应被转换。"""
        assert ProcessManager._convert_windows_paths_for_wsl("cat ./file.txt") == "cat ./file.txt"

    def test_unix_path_not_converted(self):
        """已经是 Unix 风格的路径不应被转换。"""
        assert ProcessManager._convert_windows_paths_for_wsl("cat /mnt/d/file.txt") == "cat /mnt/d/file.txt"

    def test_network_share_not_converted(self):
        """网络共享路径不应被转换。"""
        assert ProcessManager._convert_windows_paths_for_wsl(r"ls \\server\share") == r"ls \\server\share"

    def test_env_variable_not_converted(self):
        """环境变量路径不应被转换。"""
        assert ProcessManager._convert_windows_paths_for_wsl("cat $HOME/file.txt") == "cat $HOME/file.txt"

    # ---- URL 不应被当作盘符路径转换 ----
    # 回归：_WIN_UNQUOTED_PATH_RE 第三分支 [a-zA-Z]:[/\\]... 曾把 URL 里
    # 紧跟冒号的字母当成盘符（https://sh.rustup.rs → http/mnt/s//sh.rustup.rs），
    # 导致 WSL 路径下所有 curl/wget/git/pip 拉取 URL 的命令全被破坏。
    # 真实事故记录见 docker/agentos Rust 安装踩坑复盘（Q2）。

    def test_https_url_not_converted(self):
        """https URL 不应被破坏（最经典的事故 case）。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl("curl https://sh.rustup.rs -o /tmp/x")
            == "curl https://sh.rustup.rs -o /tmp/x"
        )

    def test_git_clone_url_not_converted(self):
        """git clone 的 https URL 不应被破坏。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl("git clone https://github.com/foo/bar.git")
            == "git clone https://github.com/foo/bar.git"
        )

    def test_pip_index_url_not_converted(self):
        """pip -i 镜像源 URL 不应被破坏。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl(
                "pip install -i https://pypi.tuna.tsinghua.edu.cn/simple/ pkg"
            )
            == "pip install -i https://pypi.tuna.tsinghua.edu.cn/simple/ pkg"
        )

    def test_quoted_url_not_converted(self):
        """引号包裹的 URL 也不应被破坏（quoted 正则不挡，靠 unquoted 回查兜底）。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl('curl "https://sh.rustup.rs"')
            == 'curl "https://sh.rustup.rs"'
        )

    def test_url_with_port_not_converted(self):
        """带端口的 URL（第二个冒号）不应被破坏。"""
        assert (
            ProcessManager._convert_windows_paths_for_wsl("http://example.com:8080/path")
            == "http://example.com:8080/path"
        )

    # ---- 平台与边界 ----

    def test_non_windows_platform_returns_unchanged(self):
        """非 Windows 平台应原样返回命令。"""
        with patch("tools.builtin.bash.process_manager.platform.system", return_value="Linux"):
            assert ProcessManager._convert_windows_paths_for_wsl(r"cat D:\file.txt") == r"cat D:\file.txt"

    def test_empty_command(self):
        """空命令应原样返回。"""
        assert ProcessManager._convert_windows_paths_for_wsl("") == ""

    def test_no_windows_path(self):
        """不含 Windows 路径的命令应原样返回。"""
        assert ProcessManager._convert_windows_paths_for_wsl("ls -la /home/user") == "ls -la /home/user"
