"""R154b：BUG-45 断点2 回归主探针——开演→开场白落聊天区。

页面已就绪（旅程1会话已开）。步骤：找/开 roleplay 面板 → 点月语卡 → 点开演 → 轮询开场白。
"""
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
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        # 面板可能未开：点顶栏「角色扮演」入口（若有）
        has_panel = pg.evaluate("""() => {
          const t = document.body.innerText || '';
          return t.indexOf('角色扮演工作坊') >= 0;
        }""")
        print("panel open:", has_panel)
        if not has_panel:
            ok = pg.evaluate("""() => {
              // 面板入口：工作区按钮排/顶栏的「角色扮演」按钮（R152 观察为面板 tab 形态）
              const els = Array.from(document.querySelectorAll('button, [role=tab], [class*=tab]'))
                .filter((e) => e.getBoundingClientRect().width > 0
                    && (e.textContent || '').trim().indexOf('角色扮演') >= 0);
              if (!els.length) return 'no-entry';
              els[0].click();
              return 'clicked:' + (els[0].textContent || '').trim().slice(0, 20);
            }""")
            print("panel entry:", ok)
            pg.wait_for_timeout(3500)
        frames = [f for f in pg.frames if f != pg.main_frame]
        print("frames:", len(frames))
        if not frames:
            pg.screenshot(path="gui-test-screenshots/packtest_r154_no_frame.png")
            print("STILL NO FRAME")
            return
        frame = frames[0]
        # 点月语卡（若详情未展开）
        detail = frame.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button'))
            .filter((e) => (e.textContent || '').trim() === '以此角色开演' && e.getBoundingClientRect().width > 0);
          return els.length > 0;
        }""")
        print("detail open:", detail)
        if not detail:
            frame.evaluate("""() => {
              const els = Array.from(document.querySelectorAll('*'))
                .filter((e) => e.children.length === 0 && (e.textContent || '').includes('月语'));
              if (!els.length) return;
              let t = els[0];
              for (let i = 0; i < 4 && t.parentElement; i++) {
                const r = t.getBoundingClientRect();
                if (r.height > 40) break;
                t = t.parentElement;
              }
              t.click();
            }""")
            time.sleep(2.5)
        ck = frame.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button'))
            .filter((e) => (e.textContent || '').trim() === '以此角色开演' && e.getBoundingClientRect().width > 0);
          if (!els.length) return 'no-btn';
          els[0].click();
          return 'clicked';
        }""")
        print("kaiyan click:", ck)
        if ck != "clicked":
            pg.screenshot(path="gui-test-screenshots/packtest_r154_click_fail.png")
            return
        # 轮询聊天区开场白
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
                len: t.length,
                tail: t.slice(-240),
              };
            }""")
            if st["opening"]:
                opening = True
                pg.wait_for_timeout(2500)
                break
        pg.screenshot(path="gui-test-screenshots/packtest_r154_kaiyan_regression.png")
        print("OPENING VISIBLE:", opening)
        print("tail:", st.get("tail", "").replace(chr(10), " | ")[-200:])
        b.close()


main()
