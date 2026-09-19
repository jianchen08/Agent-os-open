"""R158b：轻量 GUI 证据（短超时版）。"""
import sys
import time

sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright


def connect(p):
    return p.chromium.connect_over_cdp("http://[::1]:9222", timeout=15000)


def main() -> None:
    with sync_playwright() as p:
        b = connect(p)
        pg = next(x for x in b.contexts[0].pages if "localhost:6390" in x.url)
        pg.wait_for_timeout(1500)
        st = pg.evaluate("""() => {
          const t = document.body.innerText || '';
          return {login: !!document.querySelector('input[type=password]'), len: t.length, head: t.slice(0, 80)};
        }""")
        print("state:", {k: v for k, v in st.items() if k != 'head'})
        if st["login"]:
            ins = pg.query_selector_all("input")
            ins[0].fill("admin")
            ins[1].fill("admin12345")
            (pg.query_selector("button[type=submit]")).click()
            pg.wait_for_timeout(6000)
        # 角色扮演面板入口（顶栏 tab）
        pg.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button, [role=tab]'))
            .filter((e) => e.getBoundingClientRect().width > 0 && (e.textContent || '').indexOf('角色扮演') >= 0);
          if (els.length) els[0].click();
        }""")
        pg.wait_for_timeout(5000)
        frames = [f for f in pg.frames if f != pg.main_frame]
        cards = "no-frame"
        if frames:
            cards = frames[0].evaluate("""() => {
              const t = document.body.innerText || '';
              return ['凯尔·铁手','塞拉菲娜·月语','星野未来'].map((k) => t.indexOf(k) >= 0).join(',');
            }""")
        print("cards visible (kael,luna,hoshino):", cards)
        pg.screenshot(path="gui-test-screenshots/packtest_r158_userroot_cards.png", timeout=15000)
        print("shot saved")
        b.close()


main()
