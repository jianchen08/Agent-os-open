"""R153：roleplay 开演面板重试（R152 被残留审批卡挡住，卡已清）。

配方：搜索框输入会话名 → 真实点击结果 → 截图 → 检查面板标记（开演/角色卡/世界书）。
"""
import sys
import time

sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

KEYWORD = "旅程1"


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
        # 登录兜底
        if pg.query_selector("input[type=password]"):
            ins = pg.query_selector_all("input")
            ins[0].fill("admin")
            ins[1].fill("admin12345")
            (pg.query_selector("button[type=submit]") or pg.query_selector('button:has-text("登录")')).click()
            pg.wait_for_timeout(5000)
        # 搜索会话
        sb = pg.query_selector('input[placeholder*="搜索"]') or pg.query_selector('input[type=search]')
        if not sb:
            print("no-searchbox")
            return
        sb.click()
        sb.fill(KEYWORD)
        pg.wait_for_timeout(2500)
        # 真实点击第一条结果
        clicked = pg.evaluate("""(kw) => {
          const items = Array.from(document.querySelectorAll('li, [role=option], [class*=result], [class*=item], [class*=session]'))
            .filter((e) => e.getBoundingClientRect().width > 0 && (e.innerText || '').includes(kw));
          if (!items.length) return 'no-item';
          const r = items[0].getBoundingClientRect();
          const el = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + r.height / 2, window.innerHeight - 10));
          if (el) { el.click(); return 'clicked:' + (el.textContent || '').slice(0, 40); }
          return 'no-point';
        }""", KEYWORD)
        print("click:", clicked)
        pg.wait_for_timeout(3500)
        pg.screenshot(path="gui-test-screenshots/packtest_r153_roleplay_panel.png")
        # 检查面板标记
        marks = pg.evaluate("""() => {
          const t = document.body.innerText || '';
          const has = (k) => t.indexOf(k) >= 0;
          return {
            kaiyan: has('开演'),
            jueseka: has('角色卡'),
            shijieshu: has('世界书'),
            panel_hint: has('roleplay') || has('剧场'),
            approval_card: /仅本次|拒绝执行/.test(t),
          };
        }""")
        print("panel marks:", marks)
        b.close()


main()
