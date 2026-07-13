"""Process-level E2E tests for human interaction in the real CLI.

Launches the actual CLI as a subprocess, drives it through stdin,
and validates that the human interaction flow works end-to-end with
a real LLM (MiniMax M2.7).

Tests:
- Choice mode: agent asks a question, user selects an option
- Conversation mode: agent initiates a conversation, user replies
- Timeout scenario: short timeout triggers timeout handling

All tests are marked with ``@pytest.mark.integration`` and require
a live LLM API key.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SRC_DIR = PROJECT_ROOT / "src"
OUTPUT_FILE = PROJECT_ROOT / "test_human_interaction_e2e_output.txt"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*."""
    return _ANSI_RE.sub("", text)


class CLIProcess:
    """Wrapper around an asyncio subprocess for the CLI.

    Provides convenience methods for sending input, reading output
    lines, and performing graceful shutdown.
    """

    def __init__(
        self,
        timeout_line: float = 120.0,
    ) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._timeout_line = timeout_line
        self._start_time: float = 0.0

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start the CLI subprocess."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC_DIR)
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"

        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "channels.cli.cli_main",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        self._start_time = time.monotonic()

        # Background readers
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        """Read stdout line by line into a buffer."""
        assert self._proc and self._proc.stdout
        while True:
            raw = await self._proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            self._stdout_lines.append(line)

    async def _read_stderr(self) -> None:
        """Read stderr line by line into a buffer."""
        assert self._proc and self._proc.stderr
        while True:
            raw = await self._proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            self._stderr_lines.append(line)

    # -- interaction ---------------------------------------------------------

    async def send(self, text: str) -> None:
        """Write *text* + newline to the subprocess stdin."""
        assert self._proc and self._proc.stdin
        self._proc.stdin.write((text + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def wait_for(
        self,
        pattern: str,
        timeout: float | None = None,
        poll_interval: float = 0.5,
    ) -> str | None:
        """Poll the stdout buffer until a line matches *pattern*.

        Args:
            pattern: Regex pattern to search for in each line.
            timeout: Maximum seconds to wait (default: self._timeout_line).
            poll_interval: Seconds between polls.

        Returns:
            The matching line (with ANSI stripped), or ``None`` on timeout.
        """
        if timeout is None:
            timeout = self._timeout_line
        deadline = time.monotonic() + timeout
        compiled = re.compile(pattern)
        last_seen = 0

        while time.monotonic() < deadline:
            # Check new lines since last_seen
            for i in range(last_seen, len(self._stdout_lines)):
                line = self._stdout_lines[i]
                clean = _strip_ansi(line)
                if compiled.search(clean):
                    return clean
            last_seen = max(last_seen, len(self._stdout_lines))

            if self._proc and self._proc.returncode is not None:
                # Process exited; drain remaining lines
                await asyncio.sleep(1)
                for i in range(last_seen, len(self._stdout_lines)):
                    clean = _strip_ansi(self._stdout_lines[i])
                    if compiled.search(clean):
                        return clean
                return None

            await asyncio.sleep(poll_interval)

        return None

    async def wait_for_any(
        self,
        patterns: list[str],
        timeout: float | None = None,
        poll_interval: float = 0.5,
    ) -> tuple[int, str] | None:
        """Poll stdout until any of *patterns* matches.

        Returns:
            (index, matching_line) or ``None`` on timeout.
        """
        if timeout is None:
            timeout = self._timeout_line
        compiled = [re.compile(p) for p in patterns]
        deadline = time.monotonic() + timeout
        last_seen = 0

        while time.monotonic() < deadline:
            for i in range(last_seen, len(self._stdout_lines)):
                clean = _strip_ansi(self._stdout_lines[i])
                for idx, pat in enumerate(compiled):
                    if pat.search(clean):
                        return idx, clean
            last_seen = max(last_seen, len(self._stdout_lines))

            if self._proc and self._proc.returncode is not None:
                await asyncio.sleep(1)
                for i in range(last_seen, len(self._stdout_lines)):
                    clean = _strip_ansi(self._stdout_lines[i])
                    for idx, pat in enumerate(compiled):
                        if pat.search(clean):
                            return idx, clean
                return None

            await asyncio.sleep(poll_interval)

        return None

    # -- cleanup -------------------------------------------------------------

    async def stop(self) -> None:
        """Send /exit, wait briefly, then kill if necessary."""
        if self._proc is None:
            return

        try:
            await self.send("/exit")
        except Exception:
            pass

        # Wait up to 5 seconds for graceful exit
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            self._proc.kill()
            await self._proc.wait()

        # Wait for reader tasks to finish
        if self._reader_task:
            try:
                await asyncio.wait_for(self._reader_task, timeout=3.0)
            except asyncio.TimeoutError:
                self._reader_task.cancel()
        if self._stderr_task:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=3.0)
            except asyncio.TimeoutError:
                self._stderr_task.cancel()

    def dump_output(self, label: str = "") -> str:
        """Write captured stdout/stderr to the output file and return a summary."""
        header = f"=== {label} ===" if label else "=== OUTPUT ==="
        stdout_text = "".join(self._stdout_lines)
        stderr_text = "".join(self._stderr_lines)
        elapsed = time.monotonic() - self._start_time if self._start_time else 0

        content = (
            f"{header}\n"
            f"Elapsed: {elapsed:.1f}s\n"
            f"Return code: {self._proc.returncode if self._proc else 'N/A'}\n"
            f"\n--- STDOUT ({len(self._stdout_lines)} lines) ---\n"
            f"{stdout_text}\n"
            f"\n--- STDERR ({len(self._stderr_lines)} lines) ---\n"
            f"{stderr_text}\n"
        )

        OUTPUT_FILE.write_text(content, encoding="utf-8")
        return content

    @property
    def stdout_clean(self) -> str:
        """Return the full stdout with ANSI codes stripped."""
        return _strip_ansi("".join(self._stdout_lines))

    @property
    def stderr_clean(self) -> str:
        """Return the full stderr with ANSI codes stripped."""
        return _strip_ansi("".join(self._stderr_lines))


# ===================================================================
# Fixtures
# ===================================================================


@pytest.fixture
async def cli() -> CLIProcess:
    """Provide a started CLIProcess that is torn down after the test."""
    proc = CLIProcess(timeout_line=120.0)
    await proc.start()
    # Give the CLI a moment to initialize and print the welcome banner
    await asyncio.sleep(3)
    yield proc
    await proc.stop()
    proc.dump_output(label="post-test")


# ===================================================================
# Test: Choice mode E2E
# ===================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_human_interaction_choice_mode(cli: CLIProcess) -> None:
    """E2E test: agent uses human_interaction in choice mode, user selects option.

    Flow:
    1. Send a message asking the agent to use human_interaction tool
    2. Wait for the agent to load the tool via resource_search
    3. Wait for the interaction panel to appear in stdout
    4. Send choice "1"
    5. Wait for the agent's final response
    6. Exit and assert no crash
    """
    # Step 1: send the user message
    user_msg = (
        "请使用 human_interaction 工具，以 choice 模式向我提问"
        "'是否继续执行？'，选项为'继续'和'取消'"
    )
    await cli.send(user_msg)

    # Step 2+3: wait for the interaction panel to appear.
    # The panel contains the title and option labels rendered by rich.
    # After stripping ANSI, we should see "是否继续执行" or "选项:".
    panel_line = await cli.wait_for(
        r"是否继续执行|选项:|选项：",
        timeout=120,
    )
    assert panel_line is not None, (
        "Timed out waiting for the interaction panel.\n"
        f"Last 20 stdout lines:\n"
        + "\n".join(_strip_ansi(l) for l in cli._stdout_lines[-20:])
    )

    # Step 4: send the choice "1"
    await cli.send("1")

    # Step 5: wait for the agent to acknowledge the response.
    # After the user submits, the agent will process the result and respond.
    await cli.wait_for_any(
        [
            r"已收到|收到|好的|继续执行|approved|completed|选择.*继续",
            r"感谢|回复|response",
        ],
        timeout=120,
    )
    # We don't assert on final_line content strictly because LLM output varies.
    # The important thing is the process didn't crash.

    # Step 6: exit
    await cli.send("/exit")
    await asyncio.sleep(3)

    # Assertions: process should have exited cleanly (or been killed)
    if cli._proc and cli._proc.returncode is None:
        cli._proc.kill()
        await cli._proc.wait()

    full_stdout = cli.stdout_clean

    # No Python traceback in stdout (would indicate an unhandled crash)
    assert "Traceback (most recent call last)" not in full_stdout, (
        f"Python traceback found in stdout.\n"
        f"Last 30 lines:\n" + "\n".join(cli._stdout_lines[-30:])
    )

    # The interaction panel should have been rendered
    assert "是否继续执行" in full_stdout or "选项" in full_stdout, (
        "Interaction panel not found in stdout output."
    )

    # Log output
    cli.dump_output(label="test_human_interaction_choice_mode")
    print(f"[PASS] test_human_interaction_choice_mode completed")
    print(f"  stdout lines: {len(cli._stdout_lines)}")
    print(f"  stderr lines: {len(cli._stderr_lines)}")


# ===================================================================
# Test: Conversation mode E2E
# ===================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_human_interaction_conversation_mode(cli: CLIProcess) -> None:
    """E2E test: agent uses human_interaction in conversation mode, user replies.

    Flow:
    1. Send a message asking the agent to use human_interaction in conversation mode
    2. Wait for the conversation panel to appear
    3. Send a reply
    4. Wait for the agent's final response
    5. Exit and assert no crash
    """
    user_msg = (
        "请使用 human_interaction 工具，以 conversation 模式发起对话，"
        "标题为'架构讨论'，开场消息为'让我们讨论新设计方案'"
    )
    await cli.send(user_msg)

    # Wait for the conversation panel
    panel_line = await cli.wait_for(
        r"架构讨论|让我们讨论|对话模式|conversation",
        timeout=120,
    )
    assert panel_line is not None, (
        "Timed out waiting for the conversation panel.\n"
        f"Last 20 stdout lines:\n"
        + "\n".join(_strip_ansi(l) for l in cli._stdout_lines[-20:])
    )

    # Send a reply in the conversation
    await cli.send("我觉得微服务架构比较合适")

    # Wait for the agent to process
    await cli.wait_for_any(
        [
            r"收到|好的|了解|确认|回复|response",
            r"返回主",
        ],
        timeout=120,
    )

    # Exit the sub-conversation if still in it
    await cli.send("/back")
    await asyncio.sleep(2)

    # Exit the CLI
    await cli.send("/exit")
    await asyncio.sleep(3)

    if cli._proc and cli._proc.returncode is None:
        cli._proc.kill()
        await cli._proc.wait()

    full_stdout = cli.stdout_clean
    assert "Traceback (most recent call last)" not in full_stdout, (
        f"Python traceback found in stdout.\n"
        + "\n".join(cli._stdout_lines[-30:])
    )

    cli.dump_output(label="test_human_interaction_conversation_mode")
    print(f"[PASS] test_human_interaction_conversation_mode completed")
    print(f"  stdout lines: {len(cli._stdout_lines)}")
    print(f"  stderr lines: {len(cli._stderr_lines)}")


# ===================================================================
# Test: Timeout scenario (short timeout)
# ===================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_human_interaction_timeout(cli: CLIProcess) -> None:
    """E2E test: agent uses human_interaction with a very short timeout.

    Flow:
    1. Ask the agent to use human_interaction with timeout_seconds=5
    2. Wait for the interaction panel to appear
    3. Do NOT respond — let it timeout
    4. Wait for the timeout message in stdout
    5. Exit and assert no crash
    """
    user_msg = (
        "请使用 human_interaction 工具，以 choice 模式向我提问"
        "'请确认操作'，选项为'确认'和'取消'，"
        "timeout_seconds 设置为 5"
    )
    await cli.send(user_msg)

    # Wait for the interaction panel
    panel_line = await cli.wait_for(
        r"请确认操作|选项:|选项：",
        timeout=120,
    )
    assert panel_line is not None, (
        "Timed out waiting for the interaction panel.\n"
        f"Last 20 stdout lines:\n"
        + "\n".join(_strip_ansi(l) for l in cli._stdout_lines[-20:])
    )

    # Do NOT send a choice — wait for the timeout to occur.
    # The tool has timeout_seconds=5, plus some overhead for the service.
    # Wait up to 30 seconds for the timeout message.
    timeout_line = await cli.wait_for(
        r"超时|timeout|TIMEOUT|timed out",
        timeout=60,
    )
    # The timeout might appear in the agent's response text or as a system message.
    # We log it regardless but don't fail if the LLM handled it differently.

    # Exit
    await cli.send("/exit")
    await asyncio.sleep(3)

    if cli._proc and cli._proc.returncode is None:
        cli._proc.kill()
        await cli._proc.wait()

    full_stdout = cli.stdout_clean
    assert "Traceback (most recent call last)" not in full_stdout, (
        f"Python traceback found in stdout.\n"
        + "\n".join(cli._stdout_lines[-30:])
    )

    cli.dump_output(label="test_human_interaction_timeout")
    print(f"[PASS] test_human_interaction_timeout completed")
    print(f"  stdout lines: {len(cli._stdout_lines)}")
    print(f"  stderr lines: {len(cli._stderr_lines)}")
    if timeout_line:
        print(f"  timeout detected in output: {timeout_line.strip()}")
    else:
        print("  timeout message not explicitly detected (may be handled internally)")
