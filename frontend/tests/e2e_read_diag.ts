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
  await p.evaluate(() => localStorage.removeItem('__ml_diag'))
  await p.reload({ waitUntil: 'domcontentloaded', timeout: 60000 })
  await p.waitForTimeout(3000)
  const diag = await p.evaluate(() => {
    const ml = document.querySelector('[data-testid=message-list]') as HTMLElement | null
    const raw = localStorage.getItem('__ml_diag')
    return {
      diag: raw ? JSON.parse(raw) : null,
      scrollTop: ml ? ml.scrollTop : -1,
      scrollHeight: ml ? ml.scrollHeight : -1,
      clientHeight: ml ? ml.clientHeight : -1,
      maxScroll: ml ? ml.scrollHeight - ml.clientHeight : -1,
      atBottom: ml ? (ml.scrollHeight - ml.scrollTop - ml.clientHeight < 50) : false,
    }
  })
  console.log('诊断结果:', JSON.stringify(diag, null, 2))
  await b.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
