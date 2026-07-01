/**
 * 刷新加载耗时测试（真实场景：刷新浏览器后页面加载慢）
 *
 * 用 CDP Network.domain 捕获刷新后所有 HTTP 请求的瀑布图，
 * 定位是哪个 API 慢，还是渲染卡。
 *
 * 场景：登录后 → 进入会话 → 刷新页面 → 测量加载耗时
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'fs'

const FRONTEND_URL = 'http://localhost:5289'

async function main() {
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })
  const page = await context.newPage()

  // 用 CDP 收集精确的网络请求耗时
  const client = await context.newCDPSession(page)
  await client.send('Network.enable')
  const reqMap = new Map<string, { url: string; method: string; start: number }>()
  const requests: any[] = []
  await client.on('Network.requestWillBeSent', (e: any) => {
    const url = e.request.url
    if (url.includes('/api/') || url.includes('messages')) {
      reqMap.set(e.requestId, { url: url.replace(/^https?:\/\/[^/]+/, ''), method: e.request.method, start: Date.now() })
    }
  })
  await client.on('Network.responseReceived', (e: any) => {
    const r = reqMap.get(e.requestId)
    if (r && !r.url.includes('undefined')) {
      const duration = Date.now() - r.start
      requests.push({ url: r.url, method: r.method, duration, status: e.response.status })
      // messages 请求单独详细打印
      if (r.url.includes('messages')) {
        console.log(`[MSG请求] ${duration}ms ${r.method} ${r.url} status=${e.response.status}`)
      }
    }
  })

  // 1. 首次登录
  console.log('=== 1. 首次登录 ===')
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle', timeout: 30000 })
  await page.fill('[data-testid="login-username-input"]', 'admin')
  await page.fill('[data-testid="login-password-input"]', 'admin123')
  await page.click('form button')
  await page.waitForTimeout(5000)
  console.log('登录完成')

  // 2. 进入"真实修仙游戏"会话
  console.log('\n=== 2. 进入修仙游戏会话 ===')
  const entered = await page.evaluate(() => {
    const spans = Array.from(document.querySelectorAll('span, div, button'))
    const target = spans.find((s) => (s.textContent || '').includes('真实修仙游戏') && s.children.length === 0)
    if (target) { ;(target as HTMLElement).click(); return true }
    return false
  })
  console.log('进入会话:', entered)
  await page.waitForTimeout(5000)
  requests.length = 0  // 清空，只测刷新后的

  // 3. 刷新页面（核心测试场景）
  console.log('\n=== 3. 刷新页面（测加载耗时）===')
  const t0 = Date.now()
  await page.reload({ waitUntil: 'networkidle', timeout: 60000 })
  const t1 = Date.now()
  console.log(`★ 页面刷新到 networkidle: ${(t1 - t0) / 1000}s`)

  // 4. 等会话恢复 + 消息加载
  console.log('等待会话恢复（10秒）...')
  await page.waitForTimeout(10000)
  const t2 = Date.now()
  console.log(`刷新后总观察: ${(t2 - t0) / 1000}s`)

  // 5. 看消息是否加载出来
  const msgCount = await page.evaluate(() => {
    // 数消息气泡
    return document.querySelectorAll('[data-testid="message-item"], [data-role]').length
  })
  console.log(`渲染的消息数: ${msgCount}`)

  // 6. 输出网络请求瀑布
  console.log(`\n=== 网络请求瀑布（共 ${requests.length} 个 API 请求）===`)
  requests.sort((a, b) => b.duration - a.duration)
  console.log('按耗时降序（前15）:')
  for (const r of requests.slice(0, 15)) {
    const slow = r.duration > 1000 ? ' ⚠️慢' : (r.duration > 300 ? ' !偏高' : '')
    console.log(`  ${r.duration.toFixed(0).padStart(6)}ms ${r.status} ${r.method} ${r.url.slice(0, 70)}${slow}`)
  }
  const slowReqs = requests.filter((r) => r.duration > 1000)
  console.log(`\n慢请求(>1s): ${slowReqs.length} 个`)
  const totalApiTime = requests.reduce((a, r) => a + r.duration, 0)
  console.log(`API总耗时（含重叠）: ${(totalApiTime / 1000).toFixed(1)}s`)

  writeFileSync('tests/e2e_reload_waterfall.json', JSON.stringify({
    reloadToIdleSec: (t1 - t0) / 1000,
    totalObserveSec: (t2 - t0) / 1000,
    msgCount,
    requestCount: requests.length,
    slowRequestCount: slowReqs.length,
    topSlow: requests.slice(0, 10),
  }, null, 2))

  await browser.close()
}

main().catch((e) => { console.error('失败:', e); process.exit(1) })
