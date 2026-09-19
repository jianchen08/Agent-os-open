"""R156a：页面状态勘查。"""
import sys

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


import time


def main() -> None:
    with sync_playwright() as p:
        b = connect(p)
        pg = next(x for x in b.contexts[0].pages if "localhost:6390" in x.url)
        pg.wait_for_timeout(1500)
        st = pg.evaluate("""() => {
          const t = document.body.innerText || '';
          return {
            login: !!document.querySelector('input[type=password]'),
            has_nav: t.indexOf('导航') >= 0 || t.indexOf('新建会话') >= 0,
            has_card_kw: ['工具调用', '监控', '会话记录', '调用记录'].map((k) => [k, t.indexOf(k) >= 0]),
            head: t.slice(0, 400),
          };
        }""")
        print({k: v for k, v in st.items() if k != "head"})
        print("head:", st["head"].replace(chr(10), " | ")[:350])
        pg.screenshot(path="gui-test-screenshots/packtest_r156_state.png")
        b.close()


main()
