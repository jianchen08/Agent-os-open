"""R154c：面板帧勘查 + 点卡。"""
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
            print("no frame")
            return
        frame = frames[0]
        txt = frame.evaluate("() => (document.body && document.body.innerText || '').slice(0, 500)")
        print("frame text:", txt.replace(chr(10), " | ")[:400])
        # 真实点击月语卡：从叶子往上找可点容器，用 frame.locator 真实点击
        try:
            loc = frame.locator("text=塞拉菲娜·月语").first
            loc.click(timeout=8000)
            print("locator click OK")
        except Exception as exc:  # noqa: BLE001
            print("locator click fail:", str(exc)[:120])
        time.sleep(3)
        pg.screenshot(path="gui-test-screenshots/packtest_r154_card_detail.png")
        btn = frame.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button'))
            .filter((e) => (e.textContent || '').trim() === '以此角色开演' && e.getBoundingClientRect().width > 0);
          return els.length;
        }""")
        print("kaiyan btn:", btn)
        if btn:
            frame.locator("button", has_text="以此角色开演").first.click(timeout=8000)
            print("kaiyan clicked via locator")
        b.close()


main()
