"""R154e：重载页面→开场白应在聊天区可见（BUG-45 断点2 GUI 判据）。"""
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
        pg.reload()
        pg.wait_for_timeout(6000)
        if pg.query_selector("input[type=password]"):
            ins = pg.query_selector_all("input")
            ins[0].fill("admin")
            ins[1].fill("admin12345")
            (pg.query_selector("button[type=submit]") or pg.query_selector('button:has-text("登录")')).click()
            pg.wait_for_timeout(6000)
        st = pg.evaluate("""() => {
          const t = document.body.innerText || '';
          return {
            opening: t.indexOf('月神之名') >= 0,
            session_visible: t.indexOf('旅程1') >= 0,
            head: t.slice(0, 150),
          };
        }""")
        print("after reload:", {k: v for k, v in st.items() if k != "head"})
        # 若首页未自动进会话，点进旅程1
        if not st["opening"]:
            sb = pg.query_selector('input[placeholder*="搜索"]')
            if sb:
                sb.click()
                sb.fill("旅程1")
                pg.wait_for_timeout(2200)
            ok = pg.evaluate("""(kw) => {
              const items = Array.from(document.querySelectorAll('div, li, [class*=session], [class*=item]'))
                .filter((e) => e.getBoundingClientRect().width > 0 && (e.innerText || '').trim().startsWith(kw));
              if (!items.length) return 'no-item';
              items[0].click();
              return 'clicked';
            }""", "旅程1·roleplay 开演")
            print("re-enter:", ok)
            pg.wait_for_timeout(4500)
            st2 = pg.evaluate("""() => {
              const t = document.body.innerText || '';
              return { opening: t.indexOf('月神之名') >= 0, tail: t.slice(-300) };
            }""")
            print("after enter:", st2["opening"])
            pg.screenshot(path="gui-test-screenshots/packtest_r154_opening_visible.png")
            tail = st2["tail"]
        else:
            pg.screenshot(path="gui-test-screenshots/packtest_r154_opening_visible.png")
            tail = st["head"]
        print("VERDICT opening visible:", st["opening"])
        b.close()


main()
