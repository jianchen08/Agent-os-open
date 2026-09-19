"""R155e：监控页 → 工具调用 tab → 按工具统计视图。"""
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
        pg.evaluate("() => { location.hash = '#/monitoring'; }")
        pg.wait_for_timeout(4000)
        # 找监控页内 tab（工具调用）
        clicked = pg.evaluate("""() => {
          const els = Array.from(document.querySelectorAll('button, [role=tab], [class*=tab]'))
            .filter((e) => e.getBoundingClientRect().width > 0 && (e.textContent || '').indexOf('工具调用') >= 0);
          if (!els.length) return 'no-el';
          els.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
          els[0].click();
          return 'clicked:' + (els[0].textContent || '').trim().slice(0, 30);
        }""")
        print("tool tab:", clicked)
        pg.wait_for_timeout(7000)
        frames = [f for f in pg.frames if f != pg.main_frame]
        print("frames:", len(frames))
        if frames:
            frame = frames[0]
            txt = frame.evaluate("() => (document.body && document.body.innerText || '')")
            print("frame len:", len(txt))
            ck = frame.evaluate("""() => {
              const els = Array.from(document.querySelectorAll('button, [role=tab], div'))
                .filter((e) => (e.textContent || '').trim() === '按工具统计' && e.getBoundingClientRect().width > 0);
              if (!els.length) return 'no-el';
              els[0].click();
              return 'clicked';
            }""")
            print("stats tab:", ck)
            time.sleep(5)
            txt2 = frame.evaluate("() => (document.body && document.body.innerText || '')")
            i = txt2.find("按工具统计")
            print("stats:", txt2[i:i+450].replace(chr(10), " | ") if i >= 0 else txt2[:280].replace(chr(10), " | "))
        b.close()


main()
