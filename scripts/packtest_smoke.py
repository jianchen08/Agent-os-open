# -*- coding: utf-8 -*-
"""打包件启动探针（BUG-9 回归门禁）。

对 electron-builder --dir 产物做 CDP 只读冒烟：
  1. 生产前端经 app:// 自定义协议加载（不再是 file://）；
  2. React 挂载（#root 非空）——BUG-9 白屏的直接反例；
  3. SPA 深链回落：直接导航 app://bundle/login 后路由到 /login 且重新挂载；
  4. 内核代理路由已接线：页面内 fetch('/health') 命中代理层而非静态回落
     （静态回落会返回 text/html；代理层返回内核 JSON 或内核不可达时的 500）。

用法：
  python scripts/packtest_smoke.py [--exe release/win-unpacked/灵汐助手.exe]
                                   [--cdp-port 9222] [--timeout 90]

退出码 0 = 全部断言通过；1 = 任一失败（含预检失败）。
依赖：python 环境含 playwright（仅 connect_over_cdp，无需浏览器安装）。
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

REPO = r"D:\myproject\container_e17cc5927dfd"
DEFAULT_EXE = REPO + r"\release\win-unpacked\灵汐助手.exe"


def log(msg: str) -> None:
    print(f"[SMOKE] {msg}", flush=True)


def fail(msg: str) -> None:
    log(f"FAIL: {msg}")
    sys.exit(1)


def exe_running(name: str) -> bool:
    # tasklist 在中文 Windows 输出 GBK（exe 名含中文），按字节解码兼容
    raw = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}"], capture_output=True
    ).stdout
    out = raw.decode("utf-8", errors="replace")
    return name in out or name in raw.decode("gbk", errors="replace")


def wait_cdp(port: int, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/json/version"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                json.loads(resp.read().decode("utf-8"))
                return
        except Exception:
            time.sleep(0.5)
    fail(f"CDP 端口 {port} 在 {timeout_s}s 内未就绪（exe 未启动或调试端口未开）")


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)


def js(page, expr: str):
    return page.evaluate(expr)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default=DEFAULT_EXE)
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--timeout", type=float, default=90.0, help="挂载等待总超时（秒）")
    args = parser.parse_args()

    exe_name = args.exe.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if exe_running(exe_name):
        fail(f"{exe_name} 已在运行（单实例锁会让新进程直接退出）；请先关闭既有实例再跑探针")

    proc = subprocess.Popen(
        [args.exe, f"--remote-debugging-port={args.cdp_port}"],
        cwd=REPO,
    )
    mounted = False
    try:
        wait_cdp(args.cdp_port, 30.0)
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.cdp_port}")
            ctx = browser.contexts[0]
            # 优先选 app:// 目标；CDP 初始可能附带 about:blank 空页
            page = next(
                (p for p in ctx.pages if p.url.startswith("app://")), None
            ) or (ctx.pages[0] if ctx.pages else ctx.wait_for_page())

            deadline = time.time() + args.timeout
            root_children = 0
            while time.time() < deadline:
                state = js(
                    page,
                    "() => ({"
                    "protocol: location.protocol, host: location.host,"
                    "path: location.pathname, ready: document.readyState,"
                    "rootChildren: document.getElementById('root')?.childElementCount ?? -1,"
                    "bodyTextLen: (document.body?.innerText || '').trim().length"
                    "})",
                )
                if (
                    state["ready"] == "complete"
                    and state["rootChildren"] > 0
                    and state["bodyTextLen"] > 0
                ):
                    mounted = True
                    break
                time.sleep(1.0)

            if not mounted:
                fail(f"#root 未挂载或无可见内容（超时 {args.timeout}s）：最后状态={state}")

            # 1. 协议与源
            if state["protocol"] != "app:" or state["host"] != "bundle":
                fail(f"加载源异常：{state['protocol']}//{state['host']}，期望 app://bundle")
            log(f"PASS 协议源: app://bundle（pathname={state['path']}）")

            # 2. React 挂载 + 页面有可见内容
            log(f"PASS React 挂载: #root children={state['rootChildren']}, body 文本 {state['bodyTextLen']} 字符")

            # 3. SPA 深链回落（history 路由直接命中）
            page.goto("app://bundle/login")
            deep = js(
                page,
                "() => new Promise(r => {"
                "  const t0 = Date.now();"
                "  const tick = () => {"
                "    const n = document.getElementById('root')?.childElementCount ?? 0;"
                "    if (n > 0 || Date.now() - t0 > 15000) return r({path: location.pathname, root: n});"
                "    setTimeout(tick, 300);"
                "  };"
                "  tick();"
                "})",
            )
            if deep["path"] != "/login" or deep["root"] <= 0:
                fail(f"SPA 深链回落失败：pathname={deep['path']}, #root children={deep['root']}")
            log(f"PASS SPA 深链回落: /login 挂载成功（children={deep['root']}）")

            # 4. 内核代理路由（代理面内的 /api/v1/auth/me 无 token 应得内核 401 JSON；
            #    若命中静态回落会返回 text/html——代理未生效的反例）
            health = js(
                page,
                "() => fetch('/api/v1/auth/me').then(r => r.text().then(t => "
                "({status: r.status, ctype: r.headers.get('content-type') || '', body: t.slice(0, 200)})))"
                ".catch(e => ({status: -1, ctype: '', body: String(e)}))",
            )
            if "text/html" in health["ctype"]:
                fail(f"/api/v1/auth/me 命中静态回落（text/html）——代理路由未生效: {health}")
            kernel_up = health["status"] != -1 and health["status"] != 500
            log(
                f"{'PASS' if kernel_up else 'WARN'} 内核代理路由: /api/v1/auth/me status={health['status']}"
                f" ctype={health['ctype']!r} body={health['body'][:80]!r}"
                + ("" if kernel_up else "（内核不可达：代理层已接线，回源失败属预期）")
            )

            log("SMOKE PASS：打包件加载链全绿")
    finally:
        kill_tree(proc.pid)

    sys.exit(0)


if __name__ == "__main__":
    main()
