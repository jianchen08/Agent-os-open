"""R153 清掉页面上的残留审批卡（拒绝不需要的旧卡），恢复 GUI 可操作性。"""
import sys
import time

sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright

IS_APPROVAL_BTN = "/仅本次|本管道内同命令免批|拒绝执行/"


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


FIND_CARD = """() => {
  const btns = Array.from(document.querySelectorAll('button'))
    .filter((b) => b.getBoundingClientRect().width > 0);
  const reject = btns.find((b) => (b.textContent || '').trim() === '拒绝执行');
  const once = btns.find((b) => (b.textContent || '').trim().indexOf('仅本次') >= 0);
  return { rejectFound: !!reject, onceFound: !!once };
}"""

CLICK_REJECT = """() => {
  const btns = Array.from(document.querySelectorAll('button'))
    .filter((b) => b.getBoundingClientRect().width > 0);
  const t = btns.find((b) => (b.textContent || '').trim() === '拒绝执行');
  if (t) { t.click(); return true; }
  return false;
}"""

CARD_GONE = """() => {
  const btns = Array.from(document.querySelectorAll('button'))
    .filter((b) => b.getBoundingClientRect().width > 0);
  return !btns.some((b) => /仅本次|拒绝执行/.test((b.textContent || '').trim()));
}"""


def main() -> None:
    with sync_playwright() as p:
        b = connect(p)
        pg = next(x for x in b.contexts[0].pages if "localhost:6390" in x.url)
        pg.wait_for_timeout(1200)
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(300)
        state = pg.evaluate(FIND_CARD)
        print("card state:", state)
        if state.get("rejectFound"):
            ok = pg.evaluate(CLICK_REJECT)
            print("clicked reject:", ok)
            time.sleep(3)
        gone = pg.evaluate(CARD_GONE)
        print("card gone:", gone)
        pg.screenshot(path="gui-test-screenshots/packtest_r153_after_clear.png")
        b.close()


main()
