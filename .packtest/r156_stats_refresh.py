"""R156c：按工具统计——点刷新读数据。"""
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
        time.sleep(2)
    raise RuntimeError(f"CDP 连接失败: {last}")


def main() -> None:
    with sync_playwright() as p:
        b = connect(p)
        pg = next(x for x in b.contexts[0].pages if "localhost:6390" in x.url)
        pg.wait_for_timeout(1200)
        frames = [f for f in pg.frames if f != pg.main_frame]
        if not frames:
            print("no frame — 页面不在工具调用记录")
            return
        frame = frames[0]
        ck = frame.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button, [role=tab], div, span'))
            .filter((e) => (e.textContent || '').trim() === '刷新' && e.getBoundingClientRect().width > 0);
          if (!els.length) return 'no-el';
          els[0].click();
          return 'clicked';
        }""")
        print("refresh:", ck)
        # 等查询完成（查询中... 消失）
        deadline = time.time() + 40
        while time.time() < deadline:
            pg.wait_for_timeout(3000)
            txt = frame.evaluate("() => (document.body && document.body.innerText || '')")
            if "查询中" not in txt:
                break
        i = txt.find("按工具统计")
        block = txt[i:i+800] if i >= 0 else txt[:400]
        print("stats block:", block.replace(chr(10), " | ")[:650])
        verdict = {
            "has_fail": ("查询失败" in txt) or ("加载超时" in txt),
            "has_rows": any(k in txt for k in ("bash_execute", "file_read", "file_write", "task_submit", "send_message", "task_evaluate", "llm")),
        }
        print("verdict:", verdict)
        pg.screenshot(path="gui-test-screenshots/packtest_r156_stats_data.png")
        b.close()


main()
