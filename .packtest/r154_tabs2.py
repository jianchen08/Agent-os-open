"""R154g：tab 测试（evaluate 点击版）。"""
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
        print("frames:", len(frames))
        if not frames:
            return
        frame = frames[0]
        for tab in ("世界书", "开演记录"):
            clicked = frame.evaluate("""(name) => {
              const els = Array.from(document.querySelectorAll('button, [role=tab], div'))
                .filter((e) => (e.textContent || '').trim() === name && e.getBoundingClientRect().width > 0);
              if (!els.length) return 'no-el';
              els[0].click();
              return 'clicked';
            }""", tab)
            print(tab, "click:", clicked)
            time.sleep(2.5)
            txt = frame.evaluate("() => (document.body.innerText || '').slice(0, 460)")
            print(f"== {tab} ==")
            print(txt.replace(chr(10), " | ")[:360])
            pg.screenshot(path=f"gui-test-screenshots/packtest_r154_tab_{tab}.png")
        b.close()


main()
