"""R153e：点击「以此角色开演」，验证开演链路（主题切换/开场白注入/开演记录）。"""
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
        if not frames:
            print("no iframe")
            return
        frame = frames[0]
        clicked = frame.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button, [role=button], a, div'))
            .filter((e) => (e.textContent || '').trim().startsWith('以此角色开演')
                    && e.getBoundingClientRect().width > 0);
          if (!els.length) return 'no-btn';
          els[0].click();
          return 'clicked';
        }""")
        print("kaiyan click:", clicked)
        time.sleep(6)
        pg.screenshot(path="gui-test-screenshots/packtest_r153_kaiyan.png")
        # 主聊天区与面板状态
        main_state = pg.evaluate("""() => {
          const chat = document.querySelector('[data-region=chat]');
          const t = chat ? chat.innerText : (document.body.innerText || '');
          return {
            has_opening: t.indexOf('以月神之名') >= 0 || t.indexOf('月神之名') >= 0,
            has_typing: !!document.querySelector('[aria-label=停止生成]'),
            tail: t.slice(-260),
          };
        }""")
        print("main:", {k: v for k, v in main_state.items() if k != "tail"})
        print("tail:", main_state["tail"].replace("\n", " | ")[-220:])
        frame_state = frame.evaluate("""() => {
          const t = document.body.innerText || '';
          return { record_hint: t.slice(0, 900) };
        }""")
        print("panel:", frame_state["record_hint"].replace("\n", " | ")[:400])
        b.close()


main()
