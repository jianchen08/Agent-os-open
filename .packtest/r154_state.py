"""R154a：页面状态勘查。"""
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
        pg.wait_for_timeout(1500)
        state = pg.evaluate("""() => {
          const t = document.body.innerText || '';
          return {
            login_page: !!document.querySelector('input[type=password]'),
            has_search: !!document.querySelector('input[placeholder*="搜索"]'),
            session_listed: t.indexOf('旅程1') >= 0,
            approval: /仅本次|拒绝执行/.test(t),
            body_head: t.slice(0, 220),
          };
        }""")
        print({k: v for k, v in state.items() if k != "body_head"})
        print("head:", state["body_head"].replace(chr(10), " | ")[:200])
        pg.screenshot(path="gui-test-screenshots/packtest_r154_state.png")
        b.close()


main()
