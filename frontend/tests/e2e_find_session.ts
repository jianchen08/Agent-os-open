import { chromium } from 'playwright'

async function main() {
  const browser = await chromium.launch({ headless: true })
  const page = await (await browser.newContext()).newPage()
  await page.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })
  const li = await page.locator('[data-testid="login-username-input"]').count()
  if (li > 0) {
    await page.fill('[data-testid="login-username-input"]', 'admin')
    await page.fill('[data-testid="login-password-input"]', 'admin123')
    await page.click('form button')
    await page.waitForTimeout(4000)
  }

  // 探测会话列表项：找包含会话标题的可点击元素
  console.log('=== 探测会话列表 ===')
  // 方式1: 带会话标题文本的元素
  const titles = ['延迟测试', '四段追踪', '测试一下']
  for (const t of titles) {
    const el = page.locator(`text="${t}"`).first()
    const cnt = await el.count()
    if (cnt > 0) {
      const visible = await el.isVisible().catch(() => false)
      const tag = await el.evaluate((n: any) => n.tagName).catch(() => '?')
      const parent = await el.evaluate((n: any) => n.parentElement?.tagName + '.' + (n.parentElement?.className||'').slice(0,40)).catch(() => '?')
      console.log(`  "${t}": count=${cnt} visible=${visible} tag=${tag} parent=${parent}`)
    }
  }

  // 方式2: 找 role=listitem / button 含会话名
  console.log('=== 列表项结构 ===')
  const listitems = await page.locator('[role="listitem"], li, [class*="session" i]').count()
  console.log('listitem/li/session元素:', listitems)

  // 方式3: 直接看侧边栏所有可点击元素的文本
  console.log('=== 点击"延迟测试"会话项 ===')
  // 用文本定位 + force click（绕过可见性检查，因为可能被遮挡但实际可点）
  try {
    // 找最具体的：会话标题所在的容器
    const sessionEl = page.locator('text="延迟测试"').first()
    await sessionEl.click({ timeout: 5000, force: true })
    console.log('点击成功(force)')
    await page.waitForTimeout(3000)
    console.log('点击后URL:', page.url())
    const ta = await page.locator('textarea').count()
    console.log('点击后textarea数:', ta)
    if (ta > 0) {
      const ph = await page.locator('textarea').first().getAttribute('placeholder')
      console.log('textarea placeholder:', ph)
    }
  } catch (e: any) {
    console.log('点击失败:', e.message.slice(0, 100))
    // fallback: 滚动到元素
    try {
      await page.locator('text="延迟测试"').first().scrollIntoViewIfNeeded({ timeout: 3000 })
      await page.locator('text="延迟测试"').first().click({ timeout: 5000 })
      console.log('滚动后点击成功')
      await page.waitForTimeout(3000)
      console.log('textarea数:', await page.locator('textarea').count())
    } catch (e2: any) {
      console.log('滚动后仍失败:', e2.message.slice(0, 80))
    }
  }
  await browser.close()
}
main().catch(e => { console.error(e); process.exit(1) })
