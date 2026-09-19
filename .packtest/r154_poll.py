"""R154d：开演后轮询聊天区开场白（BUG-45 断点2 回归判据）。"""
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
        deadline = time.time() + 115
        opening = False
        st = {}
        while time.time() < deadline:
            pg.wait_for_timeout(8000)
            st = pg.evaluate("""() => {
              const chat = document.querySelector('[data-region=chat]');
              const t = chat ? chat.innerText : (document.body.innerText || '');
              return {
                opening: t.indexOf('月神之名') >= 0,
                busy: !!document.querySelector('[aria-label=停止生成]'),
                tail: t.slice(-260),
              };
            }""")
            if st["opening"]:
                opening = True
                pg.wait_for_timeout(2500)
                break
        pg.screenshot(path="gui-test-screenshots/packtest_r154_kaiyan_result.png")
        print("OPENING VISIBLE:", opening)
        print("tail:", st.get("tail", "").replace(chr(10), " | ")[-220:])
        b.close()


main()
