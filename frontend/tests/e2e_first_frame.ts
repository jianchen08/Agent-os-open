import { chromium } from 'playwright'

async function main() {
  const b = await chromium.launch({ headless: false })
  const p = await (await b.newContext({ viewport: null })).newPage()
  await p.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })
  await p.fill('[data-testid="login-username-input"]', 'admin')
  await p.fill('[data-testid="login-password-input"]', 'admin123')
  await p.click('form button')
  await p.waitForTimeout(5000)
  await p.evaluate(() => {
    const s = Array.from(document.querySelectorAll('span,div,button'))
    const t = s.find((x: any) => (x.textContent || '').includes('真实修仙游戏') && x.children.length === 0)
    if (t) (t as HTMLElement).click()
  })
  await p.waitForTimeout(3000)
  console.log('=== 刷新，高频采样前1.5秒 ===')
  await p.reload({ waitUntil: 'domcontentloaded', timeout: 60000 })
  // 立即高频采样
  const t0 = Date.now()
  const samples: any[] = []
  for (let i = 0; i < 30; i++) {
    const s = await p.evaluate(() => {
      const ml = document.querySelector('[data-testid=message-list]') as HTMLElement | null
      if (!ml) return null
      return {
        st: ml.scrollTop,
        sh: ml.scrollHeight,
        ch: ml.clientHeight,
        atBottom: ml.scrollHeight - ml.scrollTop - ml.clientHeight < 80,
      }
    }).catch(() => null)
    samples.push({ t: Date.now() - t0, ...(s || {}) })
    await p.waitForTimeout(50)
  }
  console.log('t(ms) | scrollTop | scrollH | clientH | atBottom')
  let prev: any = null
  for (const s of samples) {
    if (s.st === undefined) continue
    if (!prev || Math.abs(s.st - prev.st) > 2 || Math.abs(s.sh - prev.sh) > 2 || s.atBottom !== prev.atBottom) {
      console.log(`  ${String(s.t).padStart(5)} | ${String(s.st).padStart(8)} | ${String(s.sh).padStart(7)} | ${String(s.ch).padStart(7)} | ${s.atBottom}`)
    }
    prev = s
  }
  await b.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
