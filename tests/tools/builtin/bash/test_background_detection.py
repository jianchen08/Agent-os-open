"""检测命令里的手动后台化模式（nohup/setsid/disown/&），软提醒而非硬拦。

设计意图：本工具自带后台执行+轮询语义。模型若手动 nohup/setsid/disown/&，
进程会脱离 ProcessManager 管理（continue/terminate 看不见），沦为孤儿。
SecurityChecker 对这些模式返回 warning 提醒，但不阻断（属于合法但反模式）。
"""

from __future__ import annotations

from tools.builtin.bash.tool import SecurityChecker


class TestBackgroundDetection:
    """手动后台化模式检测。"""

    def setup_method(self):
        self.checker = SecurityChecker()

    def _check(self, cmd: str) -> tuple[bool, bool, str | None]:
        return self.checker.check(cmd)

    # ── 命中后台化模式 → warning（不阻断）──────────────────────────

    def test_nohup_warns(self):
        safe, warn, _msg = self._check("nohup cargo build &")
        assert safe is True, "nohup 不应硬拦"
        assert warn is True, "nohup 应触发 warning"

    def test_setsid_warns(self):
        safe, warn, _msg = self._check("setsid bash -c 'sleep 100'")
        assert safe is True, "setsid 不应硬拦"
        assert warn is True, "setsid 应触发 warning"

    def test_disown_warns(self):
        safe, warn, _msg = self._check("sleep 100; disown")
        assert safe is True, "disown 不应硬拦"
        assert warn is True, "disown 应触发 warning"

    def test_trailing_ampersand_warns(self):
        safe, warn, _msg = self._check("cargo build &")
        assert safe is True, "行尾 & 不应硬拦"
        assert warn is True, "行尾 & 应触发 warning"

    def test_trailing_ampersand_with_redirect_warns(self):
        """cmd > log & 这种后台+重定向也应命中"""
        safe, warn, _msg = self._check("cargo build > build.log 2>&1 &")
        assert safe is True
        assert warn is True

    # ── 正常命令不误伤 ────────────────────────────────────────────

    def test_normal_command_no_warn(self):
        safe, warn, _msg = self._check("ls -la")
        assert safe is True
        assert warn is False, "ls 不应触发 warning"

    def test_grep_nohup_as_search_content(self):
        """grep nohup file 里 nohup 是搜索关键词，不应命中后台模式。

        这是词边界检测的关键：nohup 作为命令（行首/管道后）才算后台化，
        作为 grep 的参数不算。
        """
        safe, warn, _msg = self._check("grep nohup /var/log/syslog")
        assert safe is True
        assert warn is False, "grep 的 nohup 参数不应误伤"

    def test_ampersand_in_word_not_warned(self):
        """foo&bar 这种词中 &（非行尾独立后台符）不应命中后台模式。"""
        safe, warn, _msg = self._check("echo 'a&b'")
        assert safe is True
        assert warn is False, "词中 & 不应误伤"

    def test_pip_install_no_warn(self):
        """pip install 是正常命令，不应触发后台 warning"""
        safe, warn, _msg = self._check("pip install requests")
        assert safe is True
        assert warn is False

    def test_command_with_env_prefix_no_warn(self):
        """带环境变量前缀的正常命令不应误伤"""
        safe, warn, _msg = self._check("FOO=bar python script.py")
        assert safe is True
        assert warn is False
