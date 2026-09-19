"""R153c：列出所有 frame + 面板再定位。"""
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
        print("contexts:", len(b.contexts))
        for ctx in b.contexts:
            for pg in ctx.pages:
                print("page:", pg.url[:90])
                for f in pg.frames:
                    if f != pg.main_frame:
                        print("  frame:", f.url[:120], "| name:", f.name)
        # 找主页面截图看当前状态
        pages = [x for c in b.contexts for x in c.pages if "localhost:6390" in x.url]
        if pages:
            pg = pages[0]
            # 面板可能要重开：检查右侧面板是否存在
            state = pg.evaluate("""() => {
              const t = document.body.innerText || '';
              return {
                has_panel_btn: t.indexOf('角色扮演') >= 0,
                session_visible: t.indexOf('旅程1') >= 0,
                approval: /仅本次|拒绝执行/.test(t),
              };
            }""")
            print("state:", state)
            pg.screenshot(path="gui-test-screenshots/packtest_r153_state_check.png")
        b.close()


main()
