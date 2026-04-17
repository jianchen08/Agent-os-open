"""CLI 端到端测试 - 详细日志模式"""
import subprocess
import sys
import threading
import time

all_lines = []

def read_stdout(proc, stop_event):
    while not stop_event.is_set():
        line = proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            all_lines.append(f"[OUT] {text}")
            print(f"[OUT] {text}", flush=True)

def read_stderr(proc, stop_event):
    while not stop_event.is_set():
        line = proc.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if text:
            all_lines.append(f"[ERR] {text}")
            print(f"[ERR] {text}", flush=True)

def main():
    user_input = "请帮我调研一下 Rust 语言在 Web 后端开发中的应用现状"
    timeout = 600

    env = {k: v for k, v in __import__("os").environ.items()}

    proc = subprocess.Popen(
        [sys.executable, "-m", "channels.cli.cli_main"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=r"d:\Jianguoyun\Agent os",
        env=env,
    )

    stop = threading.Event()

    t_out = threading.Thread(target=read_stdout, args=(proc, stop), daemon=True)
    t_err = threading.Thread(target=read_stderr, args=(proc, stop), daemon=True)
    t_out.start()
    t_err.start()

    time.sleep(3)

    print(f"[INPUT] {user_input}", flush=True)
    try:
        proc.stdin.write((user_input + "\n").encode("utf-8"))
        proc.stdin.flush()
    except Exception as e:
        print(f"[WARN] stdin write error: {e}")

    time.sleep(2)

    print("[INPUT] <EOF>", flush=True)
    try:
        proc.stdin.close()
    except Exception:
        pass

    try:
        proc.wait(timeout=timeout)
        print(f"\n[DONE] exit code: {proc.returncode}", flush=True)
    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"\n[TIMEOUT] killed after {timeout}s", flush=True)

    stop.set()
    t_out.join(timeout=5)
    t_err.join(timeout=5)

    print("\n===== 日志分析 =====", flush=True)
    iteration_lines = [l for l in all_lines if "Pipeline iteration" in l or "迭代" in l]
    print(f"迭代相关日志 ({len(iteration_lines)} 条):", flush=True)
    for l in iteration_lines:
        print(f"  {l}", flush=True)

    tool_lines = [l for l in all_lines if "tool_call:" in l or "Tool not found" in l or "Executed" in l]
    print(f"\n工具相关日志 ({len(tool_lines)} 条):", flush=True)
    for l in tool_lines:
        print(f"  {l}", flush=True)

    error_lines = [l for l in all_lines if "ERROR" in l or "MinimaxException" in l]
    print(f"\n错误日志 ({len(error_lines)} 条):", flush=True)
    for l in error_lines:
        print(f"  {l}", flush=True)

    route_lines = [l for l in all_lines if "Route arbitrated" in l or "route" in l.lower()]
    print(f"\n路由日志 ({len(route_lines)} 条):", flush=True)
    for l in route_lines[-20:]:
        print(f"  {l}", flush=True)

if __name__ == "__main__":
    main()
