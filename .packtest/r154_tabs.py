"""R154f：roleplay 面板「世界书」「开演记录」tab 首测。"""
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
        pg.wait_for_timeout(1200)
        frames = [f for f in pg.frames if f != pg.main_frame]
        if not frames:
            print("no panel frame — 面板未开")
            return
        frame = frames[0]
        # 世界书 tab
        try:
            frame.locator("text=世界书").first.click(timeout=6000)
            time.sleep(2)
            t1 = frame.evaluate("() => (document.body.innerText || '').slice(0, 400)")
            print("== 世界书 tab ==")
            print(t1.replace(chr(10), " | ")[:320])
            pg.screenshot(path="gui-test-screenshots/packtest_r154_lorebook_tab.png")
        except Exception as exc:  # noqa: BLE001
            print("世界书 click fail:", str(exc)[:100])
        # 开演记录 tab
        try:
            frame.locator("text=开演记录").first.click(timeout=6000)
            time.sleep(2)
            t2 = frame.evaluate("() => (document.body.innerText || '').slice(0, 500)")
            print("== 开演记录 tab ==")
            print(t2.replace(chr(10), " | ")[:380])
            pg.screenshot(path="gui-test-screenshots/packtest_r154_sessions_tab.png")
        except Exception as exc:  # noqa: BLE001
            print("开演记录 click fail:", str(exc)[:100])
        b.close()


main()
