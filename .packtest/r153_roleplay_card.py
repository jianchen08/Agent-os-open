"""R153b：roleplay 面板内交互——点角色卡（iframe 内）+ 开演记录 tab 检查。"""
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


def find_panel_frame(pg):
    for f in pg.frames:
        if "learning_panel" in (f.url or "") or "roleplay" in (f.url or "") or "sandbox" in (f.url or ""):
            return f
    return None


def main() -> None:
    with sync_playwright() as p:
        b = connect(p)
        pg = next(x for x in b.contexts[0].pages if "localhost:6390" in x.url)
        pg.wait_for_timeout(1200)
        frame = find_panel_frame(pg)
        print("panel frame:", frame.url if frame else "NOT FOUND")
        if frame is None:
            return
        # 点第二张卡（塞拉菲娜·月语，当前会话对话对象）
        cards = frame.query_selector_all("[class*=card], [class*=character], li, button")
        target = None
        for c in cards:
            try:
                if c.is_visible() and "月语" in (c.inner_text() or ""):
                    target = c
                    break
            except Exception:  # noqa: BLE001
                continue
        if target:
            target.click()
            print("clicked 月语 card")
            time.sleep(3)
        else:
            print("no 月语 card found")
        pg.screenshot(path="gui-test-screenshots/packtest_r153_roleplay_card_selected.png")
        # 面板内文本快照
        txt = frame.evaluate("() => (document.body.innerText || '').slice(0, 600)")
        print("panel text:", txt.replace("\n", " | ")[:500])
        b.close()


main()
