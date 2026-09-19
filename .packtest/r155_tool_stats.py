"""R155：工具调用记录页「按工具统计」视图数据渲染复核（T2 透项，内核空闲窗口）。"""
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
        # 导航：监控 → 工具调用记录（直接改 hash 路由最稳）
        pg.evaluate("() => { location.hash = '#/monitoring/tool-calls'; }")
        pg.wait_for_timeout(5000)
        st = pg.evaluate("""() => {
          const t = document.body.innerText || '';
          return {
            page: t.indexOf('工具调用') >= 0 || t.indexOf('按工具统计') >= 0,
            has_tabs: t.indexOf('按工具统计') >= 0 && t.indexOf('调用明细') >= 0,
            fail: t.indexOf('查询失败') >= 0 || t.indexOf('加载超时') >= 0,
            head: t.slice(0, 100),
          };
        }""")
        print("page:", st["page"], "| tabs:", st["has_tabs"], "| fail:", st["fail"])
        # 默认视图可能是调用明细——点「按工具统计」
        clicked = pg.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button, [role=tab], [class*=tab]'))
            .filter((e) => e.getBoundingClientRect().width > 0 && (e.textContent || '').trim() === '按工具统计');
          if (!els.length) return 'no-el';
          els[0].click();
          return 'clicked';
        }""")
        print("stats tab:", clicked)
        pg.wait_for_timeout(6000)
        stats = pg.evaluate("""() => {
          const t = document.body.innerText || '';
          const nums = (t.match(/\\d{3,}/g) || []).length;
          return {
            fail: t.indexOf('查询失败') >= 0 || t.indexOf('加载超时') >= 0,
            empty: t.indexOf('暂无') >= 0 || t.indexOf('0 条') >= 0,
            numeric_cells: nums,
            sample: t.slice(t.indexOf('按工具统计'), t.indexOf('按工具统计') + 400),
          };
        }""")
        print("stats fail:", stats["fail"], "| empty:", stats["empty"], "| numeric_cells:", stats["numeric_cells"])
        print("sample:", stats["sample"].replace(chr(10), " | ")[:300])
        pg.screenshot(path="gui-test-screenshots/packtest_r155_tool_stats.png")
        b.close()


main()
