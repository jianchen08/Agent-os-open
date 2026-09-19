"""R158：GUI 证据——roleplay 面板从 user_root 新源渲染三卡。"""
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
        if pg.query_selector("input[type=password]"):
            ins = pg.query_selector_all("input")
            ins[0].fill("admin")
            ins[1].fill("admin12345")
            (pg.query_selector("button[type=submit]") or pg.query_selector('button:has-text("登录")')).click()
            pg.wait_for_timeout(6000)
        # 进旅程1会话（搜索）
        sb = pg.query_selector('input[placeholder*="搜索"]')
        if sb:
            sb.click()
            sb.fill("旅程1")
            pg.wait_for_timeout(2200)
            pg.evaluate("""() => {
              const items = Array.from(document.querySelectorAll('div, li, [class*=session]'))
                .filter((e) => e.getBoundingClientRect().width > 0
                    && (e.innerText || '').trim().startsWith('旅程1'));
              if (items.length) items[0].click();
            }""")
            pg.wait_for_timeout(3500)
        frames = [f for f in pg.frames if f != pg.main_frame]
        if frames:
            txt = frames[0].evaluate("() => (document.body && document.body.innerText || '').slice(0, 220)")
            print("panel:", txt.replace(chr(10), " | ")[:200])
        pg.screenshot(path="gui-test-screenshots/packtest_r158_userroot_cards.png")
        print("shot saved")
        b.close()


main()
