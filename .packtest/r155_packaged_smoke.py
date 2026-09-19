"""R155：打包件核心冒烟（A04+A06）——app:// 登录渲染 + 发消息收精确回复。"""
import sys
import time

sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from playwright.sync_api import sync_playwright


def connect(p):
    last = None
    for _ in range(3):
        for url in ("http://127.0.0.1:9225", "http://[::1]:9225"):
            try:
                return p.chromium.connect_over_cdp(url, timeout=25000)
            except Exception as exc:  # noqa: BLE001
                last = exc
        time.sleep(3)
    raise RuntimeError(f"CDP 连接失败: {last}")


def main() -> None:
    with sync_playwright() as p:
        b = connect(p)
        pg = b.contexts[0].pages[0] if b.contexts[0].pages else b.contexts[0].new_page()
        pg.wait_for_timeout(2500)
        url = pg.url
        body_len = pg.evaluate("() => (document.body.innerText || '').length")
        print("url:", url[:60], "| body chars:", body_len)
        # 登录（打包件全新 profile 落登录页）
        if pg.query_selector("input[type=password]"):
            ins = pg.query_selector_all("input")
            ins[0].fill("admin")
            ins[1].fill("admin12345")
            (pg.query_selector("button[type=submit]") or pg.query_selector('button:has-text("登录")')).click()
            pg.wait_for_timeout(6000)
            print("logged in, url:", pg.url[:60])
        # 发消息
        r = pg.evaluate("""() => {
          const ta = document.querySelector('textarea[aria-label="消息输入"]') ||
                     document.querySelector('textarea');
          if (!ta) return 'no-textarea';
          const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
          setter.call(ta, 'R155 打包件冒烟：只回复两个字：包通');
          ta.dispatchEvent(new Event('input', {bubbles: true}));
          const btn = Array.from(document.querySelectorAll('button')).find(
            (x) => (x.getAttribute('aria-label') || '') === '发送消息');
          if (!btn) return 'no-send';
          if (btn.disabled) return 'disabled';
          btn.click();
          return 'sent';
        }""")
        print("send:", r)
        # 轮询回复
        reply = ""
        deadline = time.time() + 110
        while time.time() < deadline:
            pg.wait_for_timeout(6000)
            st = pg.evaluate("""() => {
              const chat = document.querySelector('[data-region=chat]');
              const t = chat ? chat.innerText : '';
              return {busy: !!document.querySelector('[aria-label=停止生成]'), tail: t.slice(-160)};
            }""")
            if not st["busy"] and ("包通" in st["tail"] or "收到" in st["tail"]):
                reply = st["tail"]
                break
        pg.screenshot(path="gui-test-screenshots/packtest_r155_packaged_smoke.png")
        print("REPLY OK:", bool(reply), "|", reply.replace(chr(10), " | ")[-100:])
        b.close()


main()
