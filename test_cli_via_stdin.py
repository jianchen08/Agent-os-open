"""通过 stdin 管道向真实 CLI 发送调研任务。"""

import asyncio
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


async def send_to_cli(messages: list[str], delay: float = 90):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["PYTHONIOENCODING"] = "utf-8"

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "channels.cli.cli_main",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
        env=env,
    )

    async def read_stream(stream, buffer: list):
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            buffer.append(chunk)

    stdout_buf = []
    stderr_buf = []
    stdout_task = asyncio.create_task(read_stream(proc.stdout, stdout_buf))
    stderr_task = asyncio.create_task(read_stream(proc.stderr, stderr_buf))

    for i, msg in enumerate(messages):
        print(f"\n发送消息 {i+1}/{len(messages)}: {msg}")
        try:
            proc.stdin.write((msg + "\n").encode("utf-8"))
            await proc.stdin.drain()
            print("  [OK] 消息已发送")
        except Exception as e:
            print(f"  [ERR] 发送失败: {e}")
            break

        print(f"  等待 {delay}s...")
        await asyncio.sleep(delay)

        if proc.returncode is not None:
            print(f"  进程已退出，退出码: {proc.returncode}")
            break

    try:
        proc.stdin.write(b"/exit\n")
        await proc.stdin.drain()
    except Exception:
        pass

    await asyncio.sleep(3)
    if proc.returncode is None:
        proc.kill()
        await proc.wait()

    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    stdout = b"".join(stdout_buf).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_buf).decode("utf-8", errors="replace")

    output_file = PROJECT_ROOT / "cli_test_output.txt"
    output_file.write_text(
        f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n",
        encoding="utf-8",
    )
    print(f"\n输出已保存到: {output_file}")
    return {"stdout": stdout, "stderr": stderr, "returncode": proc.returncode}


if __name__ == "__main__":
    messages = [
        "请使用 task_submit 工具，将以下任务提交给 general_agent 执行：在 .test_workspace 目录下创建一个文件 agent_test_result.txt，内容为 'Task executed by general_agent via task_submit from lingxi'",
    ]

    result = asyncio.run(send_to_cli(messages, delay=60))
    print(f"退出码: {result['returncode']}")
