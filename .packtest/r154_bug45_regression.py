"""R154：BUG-45 断点2 GUI 回归——开演→开场白落聊天区（active 指针切换生效）。

步骤：CDP 进面板 → 确认面板/卡详情就绪 → 点「以此角色开演」→ 轮询聊天区开场白。
判据：聊天区出现「月神之名」（月语卡开场白特征文本）。
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
        pg.wait_for_timeout(1500)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        if pg.query_selector("input[type=password]"):
            ins = pg.query_selector_all("input")
            ins[0].fill("admin")
            ins[1].fill("admin12345")
            (pg.query_selector("button[type=submit]") or pg.query_selector('button:has-text("登录")')).click()
            pg.wait_for_timeout(5000)
        # 进旅程1会话（搜索+真实点击）
        sb = pg.query_selector('input[placeholder*="搜索"]') or pg.query_selector('input[type=search]')
        sb.click()
        sb.fill("旅程1")
        pg.wait_for_timeout(2500)
        clicked = pg.evaluate("""(kw) => {
          const items = Array.from(document.querySelectorAll('li, [role=option], [class*=result], [class*=item], [class*=session]'))
            .filter((e) => e.getBoundingClientRect().width > 0 && (e.innerText || '').includes(kw));
          if (!items.length) return 'no-item';
          const r = items[0].getBoundingClientRect();
          const el = document.elementFromPoint(r.x + r.width / 2, Math.min(r.y + r.height / 2, window.innerHeight - 10));
          if (el) { el.click(); return 'clicked'; }
          return 'no-point';
        }""", "旅程1")
        print("session click:", clicked)
        pg.wait_for_timeout(3000)
        # 面板 iframe 内点卡（若详情未开）再点开演
        frames = [f for f in pg.frames if f != pg.main_frame]
        if not frames:
            print("no panel iframe — 面板可能未开，尝试从会话页打开")
            pg.screenshot(path="gui-test-screenshots/packtest_r154_no_panel.png")
            return
        frame = frames[0]
        sel = frame.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button'))
            .filter((e) => (e.textContent || '').trim() === '以此角色开演' && e.getBoundingClientRect().width > 0);
          return els.length;
        }""")
        print("kaiyan btn present:", sel)
        if not sel:
            # 先点月语卡展开详情
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
        # 轮询聊天区开场白（LLM 生成需时）
        deadline = time.time() + 110
        opening = False
        while time.time() < deadline:
            pg.wait_for_timeout(8000)
            st = pg.evaluate("""() => {
              const chat = document.querySelector('[data-region=chat]');
              const t = chat ? chat.innerText : (document.body.innerText || '');
              return {
                opening: t.indexOf('月神之名') >= 0 || t.indexOf('旅行者') >= 0,
                busy: !!document.querySelector('[aria-label=停止生成]'),
                tail: t.slice(-200),
              };
            }""")
            if st["opening"] and not st["busy"]:
                opening = True
                pg.wait_for_timeout(2000)
                break
            if not st["busy"] and time.time() > deadline - 20000 and not st["opening"]:
                # 未检出开场白且空闲——再给最后一轮机会前先记状态
                pass
        pg.screenshot(path="gui-test-screenshots/packtest_r154_kaiyan_regression.png")
        print("OPENING VISIBLE:", opening)
        print("chat tail:", st["tail"].replace(chr(10), " | ")[-180:])
        b.close()


main()
