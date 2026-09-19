"""R155c：工具调用统计视图——纯读取复核（无截图等待）。"""
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
        st = pg.evaluate("""() => {
          const t = document.body.innerText || '';
          return {
            url: location.hash,
            has_stats: t.indexOf('按工具统计') >= 0,
            has_detail: t.indexOf('调用明细') >= 0,
            fail: t.indexOf('查询失败') >= 0 || t.indexOf('加载超时') >= 0,
            i: t.indexOf('按工具统计'),
          };
        }""")
        print("state:", st)
        if st["i"] >= 0:
            txt = pg.evaluate("() => { const t = document.body.innerText || ''; const i = t.indexOf('按工具统计'); return t.slice(i, i + 500); }")
            print("stats block:", txt.replace(chr(10), " | ")[:400])
        try:
            pg.screenshot(path="gui-test-screenshots/packtest_r155_tool_stats2.png", timeout=8000)
            print("shot ok")
        except Exception as exc:  # noqa: BLE001
            print("shot skip:", str(exc)[:60])
        b.close()


main()
