"""R153g：开演修复回归——一次点击，验证单任务派发 + system_prompt 真实插值。

前置：play/regenerate timeout 5s->60s（user-space plugin.json）+ 卡 prompt 内联。
回归判读（外部脚本做）：
  - 该窗口 TaskSubmit「任务执行管道已创建」计数 == 1
  - 无 http endpoint timeout (504)
  - 新 checkpoint system_message 不含 {description}/{personality}/{scenario}
"""
import sys
import time

sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright


def connect(p):
    last = None
    for _ in range(3):
        for url in ("http://127.0.0.1:9222", "http://[::1]:9222"):
            try:
                return p.chromium.connect_over_cdp(url, timeout=25000)
            except Exception as exc:  # noqa: BLE001
                last = exc
        time.sleep(3)
    raise RuntimeError(f"CDP 连接失败: {last}")


def main() -> None:
    with sync_playwright() as p:
        b = connect(p)
        pg = next(x for x in b.contexts[0].pages if "localhost:6390" in x.url)
        pg.wait_for_timeout(1000)
        frames = [f for f in pg.frames if f != pg.main_frame]
        if not frames:
            print("no panel iframe")
            return
        frame = frames[0]
        clicked = frame.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button'))
            .filter((e) => (e.textContent || '').trim() === '以此角色开演'
                    && e.getBoundingClientRect().width > 0);
          if (!els.length) return 'no-btn';
          els[0].click();
          return 'clicked';
        }""")
        print("kaiyan click:", clicked)
        b.close()


main()
