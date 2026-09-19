"""R153d：经匿名 iframe 点角色卡（塞拉菲娜·月语）+ 截图。"""
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
        print("sub frames:", len(frames))
        if not frames:
            print("no iframe")
            return
        frame = frames[0]
        info = frame.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
        print("frame text head:", info.replace("\n", " | ")[:250])
        # 点月语卡
        clicked = frame.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('*'))
            .filter((e) => e.children.length === 0 && (e.textContent || '').includes('月语'));
          if (!els.length) return 'no-el';
          let t = els[0];
          for (let i = 0; i < 4 && t.parentElement; i++) {
            const r = t.getBoundingClientRect();
            if (r.height > 40) break;
            t = t.parentElement;
          }
          t.click();
          return 'clicked:' + (t.textContent || '').slice(0, 50);
        }""")
        print("click:", clicked)
        time.sleep(3)
        pg.screenshot(path="gui-test-screenshots/packtest_r153_card_clicked.png")
        after = frame.evaluate("() => (document.body.innerText || '').slice(0, 700)")
        print("after:", after.replace("\n", " | ")[:550])
        b.close()


main()
