"""R155b：工具调用统计视图复核——按用户路径导航（监控侧栏）。"""
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


def click_by_text(pg, text, exact=True):
    return pg.evaluate("""([t, ex]) => {
      const els = Array.from(document.querySelectorAll('button, [role=tab], [role=menuitem], a, li, span, div'))
        .filter((e) => {
          const s = (e.textContent || '').trim();
          return e.getBoundingClientRect().width > 0 && (ex ? s === t : s.indexOf(t) >= 0);
        });
      if (!els.length) return 'no-el';
      // 取最内层（children 最少）的可点元素
      els.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
      els[0].click();
      return 'clicked:' + (els[0].textContent || '').trim().slice(0, 24);
    }""", [text, exact])


def main() -> None:
    with sync_playwright() as p:
        b = connect(p)
        pg = next(x for x in b.contexts[0].pages if "localhost:6390" in x.url)
        pg.wait_for_timeout(1200)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        print("nav 监控:", click_by_text(pg, "监控", exact=False))
        pg.wait_for_timeout(3000)
        print("click 工具调用:", click_by_text(pg, "工具调用", exact=False))
        pg.wait_for_timeout(5000)
        st = pg.evaluate("""() => {
          const t = document.body.innerText || '';
          return {
            has_stats: t.indexOf('按工具统计') >= 0,
            has_detail: t.indexOf('调用明细') >= 0,
            fail: t.indexOf('查询失败') >= 0 || t.indexOf('加载超时') >= 0,
          };
        }""")
        print("page state:", st)
        if st["has_stats"]:
            print("click 按工具统计:", click_by_text(pg, "按工具统计"))
            pg.wait_for_timeout(6000)
            stats = pg.evaluate("""() => {
              const t = document.body.innerText || '';
              const i = t.indexOf('按工具统计');
              return {
                fail: t.indexOf('查询失败') >= 0 || t.indexOf('加载超时') >= 0,
                sample: i >= 0 ? t.slice(i, i + 500) : t.slice(0, 300),
              };
            }""")
            print("stats fail:", stats["fail"])
            print("sample:", stats["sample"].replace(chr(10), " | ")[:380])
        pg.screenshot(path="gui-test-screenshots/packtest_r155_tool_stats2.png")
        b.close()


main()
