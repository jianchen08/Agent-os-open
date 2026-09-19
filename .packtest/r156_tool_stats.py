"""R156：T2 透项——工具调用记录页「按工具统计」视图数据渲染复核。

配方（R126 沉淀）：关全部工作区 tab → 导航页点「工具调用记录」卡 → 等 iframe → 点「按工具统计」。
判据：统计视图渲染出工具名+调用计数（非空态/非查询失败）。
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
        # 1) 关全部 tab 回导航页
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
        # 2) 点导航页「工具调用记录」卡
        clicked = pg.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('div, button, a, li'))
            .filter((e) => e.getBoundingClientRect().width > 0
                && (e.textContent || '').indexOf('工具调用记录') >= 0
                && (e.textContent || '').length < 60);
          if (!els.length) return 'no-el';
          els.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
          els[0].click();
          return 'clicked';
        }""")
        print("card click:", clicked)
        # 3) 等 iframe
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
            return
        print("frame up:", frame.url[:70])
        pg.wait_for_timeout(3000)
        # 4) 点「按工具统计」
        ck = frame.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button, [role=tab], div, span'))
            .filter((e) => (e.textContent || '').trim() === '按工具统计' && e.getBoundingClientRect().width > 0);
          if (!els.length) return 'no-el';
          els[0].click();
          return 'clicked';
        }""")
        print("stats tab:", ck)
        pg.wait_for_timeout(6000)
        # 5) 读统计数据
        txt = frame.evaluate("() => (document.body && document.body.innerText || '')")
        i = txt.find("按工具统计")
        block = txt[i:i+600] if i >= 0 else txt[:400]
        print("stats block:", block.replace(chr(10), " | ")[:480])
        low = txt
        verdict = {
            "has_fail": ("查询失败" in low) or ("加载超时" in low),
            "has_empty": ("暂无" in low),
            "has_rows": any(k in low for k in ("bash_execute", "file_read", "file_write", "task_submit", "llm", "send_message")),
        }
        print("verdict:", verdict)
        pg.screenshot(path="gui-test-screenshots/packtest_r156_tool_stats.png")
        b.close()


main()
