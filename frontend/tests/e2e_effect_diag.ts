/**
 * 诊断首次定位 effect 为何不执行
 * 直接检查 DOM 状态 + 手动 pinToBottom 验证
 */
import { chromium } from 'playwright'

async function main() {
  const browser = await chromium.launch({ headless: false })
  const context = await browser.newContext({ viewport: null })
  const page = await context.newPage()

  await page.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })
  await page.fill('[data-testid="login-username-input"]', 'admin')
  await page.fill('[data-testid="login-password-input"]', 'admin123')
  await page.click('form button')
  await page.waitForTimeout(5000)

  await page.evaluate(() => {
    const spans = Array.from(document.querySelectorAll('span, div, button'))
    const t = spans.find((s) => (s.textContent || '').includes('真实修仙游戏') && s.children.length === 0)
    if (t) (t as HTMLElement).click()
  })
  await page.waitForTimeout(3000)

  // 刷新后立即高频检查 message-list 的出现时机和 scrollTop
  console.log('=== 刷新后追踪 message-list 出现时机 ===')
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 })

  // 用 page.waitForFunction 轮询，避免 evaluate 返回 Promise 的问题
  const samples: any[] = []
  const t0 = Date.now()
  for (let i = 0; i < 80; i++) {
    const s = await page.evaluate(() => {
      const ml = document.querySelector('[data-testid="message-list"]') as HTMLElement | null
      const content = ml ? (ml.querySelector(':scope > div') as HTMLElement | null) : null
      return {
        mlExists: !!ml,
        scrollTop: ml ? ml.scrollTop : -1,
        scrollHeight: ml ? ml.scrollHeight : -1,
        contentExists: !!content,
        contentHeight: content ? content.scrollHeight : -1,
        msgs: ml ? ml.querySelectorAll('[data-testid="message-item"], [data-role]').length : 0,
      }
    }).catch(() => ({}))
    samples.push({ t: Date.now() - t0, ...s })
    await page.waitForTimeout(50)
  }
  const trace = samples

  // 打印关键变化点
  console.log('t(ms) | ml存在 | scrollTop | scrollH | content存在 | contentH | msgs')
  let prev: any = null
  for (const s of trace) {
    if (!prev || s.mlExists !== prev.mlExists || s.msgs !== prev.msgs || Math.abs(s.scrollTop - prev.scrollTop) > 2 || Math.abs(s.contentHeight - prev.contentHeight) > 2) {
      console.log(`  ${String(s.t).padStart(5)} | ${String(s.mlExists).padEnd(5)} | ${String(s.scrollTop).padStart(7)} | ${String(s.scrollHeight).padStart(7)} | ${String(s.contentExists).padEnd(5)} | ${String(s.contentHeight).padStart(8)} | ${s.msgs}`)
    }
    prev = s
  }

  // 手动 pinToBottom 验证
  console.log('\n=== 手动 pinToBottom 测试 ===')
  const manualResult = await page.evaluate(() => {
    const ml = document.querySelector('[data-testid="message-list"]') as HTMLElement
    if (!ml) return 'no message-list'
    const before = ml.scrollTop
    ml.scrollTop = ml.scrollHeight
    return { before, after: ml.scrollTop, scrollHeight: ml.scrollHeight, clientHeight: ml.clientHeight }
  })
  console.log('手动钉底:', JSON.stringify(manualResult))

  await browser.close()
}

main().catch((e) => { console.error(e); process.exit(1) })
