"""R153f：开演派发后轮询聊天区开场白 + 内核侧证据。"""
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
        deadline = time.time() + 100
        result = None
        while time.time() < deadline:
            pg.wait_for_timeout(8000)
            result = pg.evaluate("""() => {
              const chat = document.querySelector('[data-region=chat]');
              const t = chat ? chat.innerText : (document.body.innerText || '');
              return {
                opening: t.indexOf('月神之名') >= 0,
                busy: !!document.querySelector('[aria-label=停止生成]'),
                msg_count: (t.match(/20小时前|分钟前|刚刚/g) || []).length,
              };
            }""")
            if result["opening"] or not result["busy"]:
                # 再给 3 秒缓冲
                pg.wait_for_timeout(3000)
                break
        pg.screenshot(path="gui-test-screenshots/packtest_r153_kaiyan_after.png")
        print("poll:", result)
        tail = pg.evaluate("""() => {
          const chat = document.querySelector('[data-region=chat]');
          const t = chat ? chat.innerText : '';
          return t.slice(-400);
        }""")
        print("chat tail:", tail.replace("\n", " | ")[-320:])
        # 面板「派发中」是否消退 + 开演记录 tab
        frames = [f for f in pg.frames if f != pg.main_frame]
        if frames:
            st = frames[0].evaluate("() => (document.body.innerText || '').slice(0, 200)")
            print("panel head:", st.replace("\n", " | ")[:180])
        b.close()


main()
