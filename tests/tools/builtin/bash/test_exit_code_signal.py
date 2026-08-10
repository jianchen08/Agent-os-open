"""退出码信号提取 + 失败消息组装的回归测试。

BUG-FIX-fix_20260802_bash_exit_code_masked:
旧实现两个问题导致 LLM 拿到的失败信息被误导：
1. 进程被信号终止（如 OOM 的 SIGKILL→137）时，exit_code 只是个孤立数字，
   工具层没把信号名/编号提取出来，LLM 不知道 137 意味着"被信号杀"。
2. 失败时用 ``output[-500:]`` 字符截断（可能切到行中间），且丢弃了
   LogCompressor 已提取的错误列表，导致 LLM 从噪音里捞原因。

修复：
- describe_exit_code(exit_code)：128+n 区间反查信号名，如实暴露。
- _build_failure_message：短输出全量、长输出按价值优先级（信号>错误列表>
  latest>tail 兜底）组装，tail_output 行级截取保证每行完整。
- ERROR_PATTERNS 补充 Killed/OOM/segfault/core dumped 识别。
"""
from __future__ import annotations

from tools.builtin.bash.log_compressor import LogCompressor
from tools.builtin.bash.tool import BashTool, describe_exit_code, tail_output


class TestDescribeExitCode:
    """退出码 → 信号描述的反查。"""

    def test_none_returns_none(self) -> None:
        assert describe_exit_code(None) is None

    def test_zero_returns_none(self) -> None:
        """正常成功不解释。"""
        assert describe_exit_code(0) is None

    def test_program_error_returns_none(self) -> None:
        """程序自身错误码（1/2/...）不在此解释（含义因程序而异）。"""
        assert describe_exit_code(1) is None
        assert describe_exit_code(2) is None
        assert describe_exit_code(127) is None

    def test_sigkill_137(self) -> None:
        """137 = 128 + 9 = SIGKILL（OOM/外部 kill 最常见）。"""
        desc = describe_exit_code(137)
        assert desc is not None
        assert "9" in desc
        assert "SIGKILL" in desc

    def test_sigterm_143(self) -> None:
        """143 = 128 + 15 = SIGTERM。"""
        desc = describe_exit_code(143)
        assert desc is not None
        assert "15" in desc
        assert "SIGTERM" in desc

    def test_sigsegv_139(self) -> None:
        """139 = 128 + 11 = SIGSEGV（段错误）。"""
        desc = describe_exit_code(139)
        assert desc is not None
        assert "SIGSEGV" in desc

    def test_out_of_known_range_returns_none(self) -> None:
        """超出 128-192 合理信号区间不解释（避免误报）。"""
        assert describe_exit_code(200) is None
        assert describe_exit_code(125) is None


class TestTailOutput:
    """尾部输出截取：字符上限但保证行完整。"""

    def test_empty(self) -> None:
        assert tail_output(None) == ""
        assert tail_output("") == ""

    def test_short_returns_full(self) -> None:
        """短于上限直接全量返回。"""
        out = "line1\nline2\nline3"
        assert tail_output(out, max_chars=100) == out

    def test_long_truncates_to_max_chars(self) -> None:
        """超长输出截取到 max_chars 以内。"""
        out = "\n".join(f"line {i}" for i in range(500))  # ~3000 chars
        result = tail_output(out, max_chars=500)
        assert len(result) <= 500
        # 末尾内容保留
        assert "line 499" in result

    def test_does_not_cut_line_middle(self) -> None:
        """截断点必须落在换行符边界，不能切到行中间。"""
        # 构造每行 100 字符的输出，截取 max_chars=150
        out = "\n".join("x" * 100 for _ in range(10))
        result = tail_output(out, max_chars=150)
        # 结果里的每一行都应该是完整的 100 个 x（或为空），不能有半行
        for line in result.split("\n"):
            assert line == "" or len(line) == 100, (
                f"行被切断：长度={len(line)}（应为 0 或 100）"
            )


class TestBuildFailureMessage:
    """_build_failure_message：失败消息按价值优先级组装。"""

    def test_signal_exit_short_output(self) -> None:
        """信号终止 + 短输出：信号描述 + 全量原文。"""
        msg, meta = BashTool._build_failure_message(
            exit_code=137,
            output="transforming...\nKilled\n",
            summary_obj={"errors": [], "latest_message": ""},
        )
        assert "SIGKILL" in msg
        assert "137" in msg
        assert "Killed" in msg  # 短输出全量
        assert meta["exit_code"] == 137
        assert meta["terminated_by_signal"] is not None

    def test_normal_error_short_output(self) -> None:
        """非信号错误 + 短输出：退出码 + 全量原文（无信号字段）。"""
        msg, meta = BashTool._build_failure_message(
            exit_code=1,
            output="error: file not found",
            summary_obj={"errors": [], "latest_message": ""},
        )
        assert "退出码: 1" in msg
        assert "file not found" in msg
        assert "terminated_by_signal" not in meta

    def test_long_output_uses_error_list(self) -> None:
        """长输出：优先用 LogCompressor 提取的错误行列表（筛掉噪音）。"""
        long_output = "\n".join(f"transforming module {i}..." for i in range(500))
        msg, _meta = BashTool._build_failure_message(
            exit_code=1,
            output=long_output,
            summary_obj={
                "errors": 1,
                "error_lines": ["error: cannot resolve dependency"],
                "latest_message": "transforming module 499...",
            },
        )
        # 错误行列表出现（高价值）
        assert "error: cannot resolve dependency" in msg
        # latest 出现
        assert "module 499" in msg

    def test_long_output_falls_back_to_tail(self) -> None:
        """长输出且无错误行：兜底用末尾原始输出（行完整）。"""
        long_output = "\n".join(f"line {i}" for i in range(500))
        msg, _meta = BashTool._build_failure_message(
            exit_code=1,
            output=long_output,
            summary_obj={"errors": 0, "error_lines": [], "latest_message": ""},
        )
        # 末尾内容保留
        assert "line 499" in msg

    def test_no_output_still_reports_exit_code(self) -> None:
        """无输出时仍报告退出码 + 信号（若有）。"""
        msg, meta = BashTool._build_failure_message(
            exit_code=137,
            output=None,
            summary_obj={},
        )
        assert "SIGKILL" in msg
        assert meta["terminated_by_signal"] is not None


class TestErrorPatternsRecognizeSignalKills:
    """LogCompressor 的 ERROR_PATTERNS 必须识别 Killed/OOM/segfault。

    否则这些行不会被计入 errors 列表，LLM 拿到 exit_code=137 却在输出里
    找不到对应错误行，误判为"命令莫名失败"。
    """

    def setup_method(self) -> None:
        self.compressor = LogCompressor()

    def test_killed_recognized(self) -> None:
        _warnings, errors = self.compressor.count_warnings_errors(["Killed"])
        assert errors == 1

    def test_out_of_memory_recognized(self) -> None:
        _warnings, errors = self.compressor.count_warnings_errors(
            ["fatal error: Out of memory"]
        )
        assert errors >= 1

    def test_oom_recognized(self) -> None:
        _warnings, errors = self.compressor.count_warnings_errors(
            ["process killed by OOM killer"]
        )
        assert errors >= 1

    def test_segfault_recognized(self) -> None:
        _warnings, errors = self.compressor.count_warnings_errors(
            ["Segmentation fault (core dumped)"]
        )
        assert errors >= 1

    def test_command_not_found_recognized(self) -> None:
        _warnings, errors = self.compressor.count_warnings_errors(
            ["bash: foo: command not found"]
        )
        assert errors >= 1
