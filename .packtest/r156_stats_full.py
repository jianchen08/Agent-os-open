"""R156b：登录 → 导航页 → 工具调用记录卡 → 按工具统计。"""
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
        time.sleep(2)
    raise RuntimeError(f"CDP 连接失败: {last}")


def main() -> None:
    with sync_playwright() as p:
        b = connect(p)
        pg = next(x for x in b.contexts[0].pages if "localhost:6390" in x.url)
        pg.wait_for_timeout(1200)
        if pg.query_selector("input[type=password]"):
            ins = pg.query_selector_all("input")
            ins[0].fill("admin")
            ins[1].fill("admin12345")
            (pg.query_selector("button[type=submit]") or pg.query_selector('button:has-text("登录")')).click()
            pg.wait_for_timeout(6000)
            print("logged in:", pg.url[:50])
        # 关残留 tab 回导航页
        for _ in range(15):
            n = pg.evaluate("""() => {
              const xbs = Array.from(document.querySelectorAll('button'))
                .filter((x) => (x.textContent || '').trim() === '\\u00d7' && x.getBoundingClientRect().width > 0);
              if (xbs.length === 0) return 0;
              xbs[0].click(); return xbs.length;
            }""")
            if n == 0:
                break
            pg.wait_for_timeout(350)
        pg.wait_for_timeout(1500)
        # 找「工具调用记录」入口（导航卡片或菜单项，放宽长度限制）
        found = pg.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('div, button, a, li, span'))
            .filter((e) => e.getBoundingClientRect().width > 0
                && (e.textContent || '').indexOf('工具调用') >= 0);
          return els.slice(0, 5).map((e) => (e.tagName || '') + ':' + (e.textContent || '').trim().slice(0, 30));
        }""")
        print("candidates:", found)
        if not found:
            pg.screenshot(path="gui-test-screenshots/packtest_r156_navpage.png")
            print("NAV CARD NOT FOUND")
            b.close()
            return
        ck = pg.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('div, button, a, li'))
            .filter((e) => e.getBoundingClientRect().width > 0
                && (e.textContent || '').indexOf('工具调用') >= 0
                && (e.textContent || '').length < 80);
          els.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
          if (!els.length) return 'no-el';
          els[0].click();
          return 'clicked';
        }""")
        print("card click:", ck)
        # 等 iframe
        frame = None
        for _ in range(20):
            pg.wait_for_timeout(1500)
            frames = [f for f in pg.frames if f != pg.main_frame]
            if frames:
                frame = frames[0]
                break
        if frame is None:
            pg.screenshot(path="gui-test-screenshots/packtest_r156_no_frame.png")
            print("NO FRAME after 30s")
            b.close()
            return
        print("frame up")
        pg.wait_for_timeout(3000)
        ck2 = frame.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button, [role=tab], div, span'))
            .filter((e) => (e.textContent || '').trim() === '按工具统计' && e.getBoundingClientRect().width > 0);
          if (!els.length) return 'no-el';
          els[0].click();
          return 'clicked';
        }""")
        print("stats tab:", ck2)
        pg.wait_for_timeout(6000)
        txt = frame.evaluate("() => (document.body && document.body.innerText || '')")
        i = txt.find("按工具统计")
        block = txt[i:i+600] if i >= 0 else txt[:400]
        print("stats block:", block.replace(chr(10), " | ")[:480])
        verdict = {
            "has_fail": ("查询失败" in txt) or ("加载超时" in txt),
            "has_empty": ("暂无" in txt),
            "has_tool_rows": any(k in txt for k in ("bash_execute", "file_read", "file_write", "task_submit", "send_message", "task_evaluate")),
        }
        print("verdict:", verdict)
        pg.screenshot(path="gui-test-screenshots/packtest_r156_tool_stats.png")
        b.close()


main()
