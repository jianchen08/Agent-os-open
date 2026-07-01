import { chromium } from 'playwright'

async function test(browserType: 'chromium' | 'msedge') {
  const b = await chromium.launch({
    headless: false,
    channel: browserType === 'msedge' ? 'msedge' : undefined,
    args: ['--start-maximized'],
  })
  const p = await (await b.newContext({ viewport: null })).newPage()

  await p.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })
  await p.fill('[data-testid="login-username-input"]', 'admin')
  await p.fill('[data-testid="login-password-input"]', 'admin123')
  await p.click('form button')
  await p.waitForTimeout(5000)
  await p.evaluate(() => {
    const s = Array.from(document.querySelectorAll('span,div,button'))
    const t = s.find((x: any) => (x.textContent||'').includes('真实修仙游戏') && x.children.length===0)
    if (t) (t as HTMLElement).click()
  })
  await p.waitForTimeout(5000)

  // 滚到 30%
  await p.evaluate(() => {
    const ml = document.querySelector('[data-testid=message-list]') as HTMLElement
    if (ml) ml.scrollTop = ml.scrollHeight * 0.3
  })
  await p.waitForTimeout(1000)

  const before = await p.evaluate(() => {
    const ml = document.querySelector('[data-testid=message-list]') as HTMLElement
    return { st: ml?.scrollTop, sr: history.scrollRestoration, ua: navigator.userAgent.match(/Edge\/[\d.]+|Chrome\/[\d.]+/)?.[0] }
  })
  console.log(`\n[${browserType}] 刷新前: st=${Math.round(before.st)} scrollRestoration=${before.sr} UA=${before.ua}`)

  await p.reload({ waitUntil: 'load', timeout: 60000 })

  // 立即采样 + 5秒后最终
  const immediate = await p.evaluate(() => {
    const ml = document.querySelector('[data-testid=message-list]') as HTMLElement
    if (!ml) return null
    return { st: ml.scrollTop, max: ml.scrollHeight - ml.clientHeight }
  }).catch(() => null)
  console.log(`[${browserType}] 刷新后立即(首个evaluate): ${immediate ? 'st='+Math.round(immediate.st)+' max='+immediate.max : 'DOM未就绪'}`)

  await p.waitForTimeout(5000)
  const final = await p.evaluate(() => {
    const ml = document.querySelector('[data-testid=message-list]') as HTMLElement
    return {
      st: ml?.scrollTop,
      max: ml ? ml.scrollHeight - ml.clientHeight : 0,
      sr: history.scrollRestoration,
    }
  })
  const atBottom = final.max - final.st < 80
  console.log(`[${browserType}] 5秒后: st=${Math.round(final.st)} max=${final.max} atBottom=${atBottom} scrollRestoration=${final.sr}`)

  await b.close()
  return atBottom
}

async function main() {
  console.log('=== 对比 Chrome vs Edge ===')
  const chromeOk = await test('chromium')
  const edgeOk = await test('msedge')
  console.log(`\n=== 结论 ===`)
  console.log(`Chrome 到底部: ${chromeOk}`)
  console.log(`Edge 到底部: ${edgeOk}`)
}
main().catch(e => { console.error(e); process.exit(1) })
