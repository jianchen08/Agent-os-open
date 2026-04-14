"""真实 LLM 测试：通过 CLI 让灵汐提交带 agent 评估器的任务，验证评估管道。"""

import asyncio
import json
import sys
import os
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

OUTPUT_DIR = PROJECT_ROOT / "test_output"
OUTPUT_DIR.mkdir(exist_ok=True)

TASKS_FILE = PROJECT_ROOT / "data" / "tasks.json"
EXEC_RECORDS_FILE = PROJECT_ROOT / "data" / "execution_records.json"


async def run_cli_with_message(message: str, wait_seconds: int = 120):
    """启动真实 CLI 进程，发送消息并等待执行完成。"""
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

    stdout_chunks = []
    stderr_chunks = []

    async def drain(stream, buf):
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            buf.append(chunk)
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines():
                print(f"  [CLI] {line[:200]}")

    t_out = asyncio.create_task(drain(proc.stdout, stdout_chunks))
    t_err = asyncio.create_task(drain(proc.stderr, stderr_chunks))

    print(f"\n{'='*60}")
    print(f"[TEST] 发送消息给灵汐: {message[:100]}...")
    print(f"[TEST] 等待 {wait_seconds} 秒让 LLM + 评估管道运行...")
    print(f"{'='*60}\n")

    try:
        proc.stdin.write((message + "\n").encode("utf-8"))
        await proc.stdin.drain()
    except Exception as e:
        print(f"[ERR] 写入失败: {e}")

    await asyncio.sleep(wait_seconds)

    try:
        proc.stdin.write(b"/exit\n")
        await proc.stdin.drain()
    except Exception:
        pass

    await asyncio.sleep(3)
    if proc.returncode is None:
        proc.kill()
        await proc.wait()

    await asyncio.gather(t_out, t_err, return_exceptions=True)

    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")

    ts = datetime.now().strftime("%H%M%S")
    out_file = OUTPUT_DIR / f"cli_eval_test_{ts}.txt"
    out_file.write_text(f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}\n", encoding="utf-8")
    print(f"\n[TEST] CLI 输出已保存: {out_file}")

    return {"stdout": stdout, "stderr": stderr}


def check_tasks_json():
    """检查 tasks.json 中的任务状态和评估结果。"""
    print(f"\n{'='*60}")
    print("[CHECK] 检查 tasks.json 任务记录")
    print(f"{'='*60}")

    if not TASKS_FILE.exists():
        print("[FAIL] tasks.json 不存在!")
        return None

    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not data:
        print("[FAIL] tasks.json 为空!")
        return None

    tasks = data if isinstance(data, list) else [data]
    found_eval = False

    for task in tasks:
        title = task.get("title", "")
        status = task.get("status", "")
        task_id = task.get("id", "")
        metadata = task.get("metadata", {})
        criteria = metadata.get("acceptance_criteria", {})
        metric_ids = metadata.get("evaluation_metric_ids", [])

        print(f"\n  任务: {title}")
        print(f"  ID: {task_id}")
        print(f"  状态: {status}")
        print(f"  评估指标 IDs: {metric_ids}")
        print(f"  验收标准: {json.dumps(criteria, ensure_ascii=False, indent=4)[:500]}")

        if metric_ids:
            found_eval = True
            agent_metrics = []
            for mid in metric_ids:
                yaml_path = PROJECT_ROOT / "config" / "evaluation_metrics" / f"{mid}.yaml"
                if yaml_path.exists():
                    import yaml
                    with open(yaml_path, "r", encoding="utf-8") as f:
                        ydata = yaml.safe_load(f)
                    etype = ydata.get("evaluator_type", "unknown")
                    ename = ydata.get("name", "")
                    print(f"    指标 {mid}: type={etype}, name={ename}")
                    if etype == "agent":
                        agent_metrics.append(mid)

            if agent_metrics:
                print(f"  ✅ 发现 AGENT 类型评估指标: {agent_metrics}")
            else:
                print(f"  ⚠️ 未发现 AGENT 类型评估指标")

        if status == "completed":
            print(f"  ✅ 任务评估通过，状态为 completed")
        elif status == "failed":
            print(f"  ❌ 任务评估失败")
        elif status == "evaluating":
            print(f"  ⏳ 任务在评估中（可能评估管道卡住）")
        elif status == "running":
            print(f"  ⏳ 任务在运行中（可能执行未完成）")
        elif status == "pending":
            print(f"  ⏳ 任务待执行（可能 TaskWorker 未启动）")

    return data


def check_eval_flow():
    """检查评估流程的关键日志。"""
    print(f"\n{'='*60}")
    print("[CHECK] 分析评估管道日志")
    print(f"{'='*60}")

    out_files = sorted(OUTPUT_DIR.glob("cli_eval_test_*.txt"))
    if not out_files:
        print("[FAIL] 没有找到 CLI 输出文件")
        return

    latest = out_files[-1]
    content = latest.read_text(encoding="utf-8")

    checks = {
        "task_submit 调用": "task_submit",
        "TaskWorker 接收任务": "TaskWorker received task",
        "任务启动 (pending→running)": "task .* started",
        "Pipeline 执行完成": "pipeline completed",
        "任务进入评估 (running→evaluating)": "moved to evaluating",
        "EvaluationExecutor 评估": "Mock agent evaluation|Mock tool evaluation|evaluation completed",
        "评估结果 (evaluating→completed/failed)": "evaluation .* passed|evaluation .* failed",
        "task_state_changed 通知": "task_state_changed",
    }

    import re
    for desc, pattern in checks.items():
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            print(f"  ✅ {desc}: 发现 {len(matches)} 处")
            for m in matches[:3]:
                print(f"      → {m[:120]}")
        else:
            print(f"  ❌ {desc}: 未找到")


async def main():
    """主测试流程。"""
    print("=" * 60)
    print("评估管道真实测试（依赖 LLM）")
    print(f"时间: {datetime.now().isoformat()}")
    print("=" * 60)

    message = "你好，请回复一句话测试"

    await run_cli_with_message(message, wait_seconds=30)

    check_tasks_json()

    check_eval_flow()

    print(f"\n{'='*60}")
    print("[DONE] 测试完成")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
