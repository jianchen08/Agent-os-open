import { chromium } from 'playwright'

async function main() {
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })

  await context.addInitScript(() => {
    ;(window as any).__patched = false
    const OrigWS = (window as any).WebSocket
    function PatchedWS(url: string, ...rest: any[]) {
      ;(window as any).__patched = true
      ;(window as any).__wsUrl = url
      return new OrigWS(url, ...rest)
    }
    PatchedWS.prototype = OrigWS.prototype
    PatchedWS.__patched = true
    ;(window as any).WebSocket = PatchedWS
  })

  const page = await context.newPage()
  await page.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })

  // 登录
  const li = await page.locator('[data-testid="login-username-input"]').count()
  if (li > 0) {
    await page.fill('[data-testid="login-username-input"]', 'admin')
    await page.fill('[data-testid="login-password-input"]', 'admin123')
    await page.click('form button')
    await page.waitForTimeout(5000)
  }

  const diag = await page.evaluate(() => ({
    patched: (window as any).__patched,
    wsUrl: (window as any).__wsUrl,
    wsIsPatched: (window as any).WebSocket?.__patched === true,
    // 检查是否有活跃 WS 连接（通过 performance entries）
    wsEntries: (performance.getEntriesByType('resource') as any[])
      .filter((e) => /ws|websocket/i.test(e.name))
      .map((e) => e.name),
  }))
  console.log('诊断:', JSON.stringify(diag, null, 2))

  await browser.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
