/**
 * 用 CDP (Chrome DevTools Protocol) 拦截 WebSocket 帧
 * CDP 的 Network.webSocketFrameReceived 能监听所有 WS 帧，无法被前端绕过。
 */
import { chromium } from 'playwright'

async function main() {
  const browser = await chromium.launch({ headless: true })
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })
  const page = await context.newPage()

  // 用 CDP session 拦截 WS
  const client = await context.newCDPSession(page)
  const wsFrames: any[] = []
  const wsSent: any[] = []
  await client.send('Network.enable')
  await client.on('Network.webSocketCreated', (e: any) => {
    console.log(`[CDP] WS创建: ${e.url}`)
  })
  await client.on('Network.webSocketFrameReceived', (e: any) => {
    try {
      const d = JSON.parse(e.response.payloadData)
      const sendTs = d.__send_ts ?? d.data?.__send_ts
      const pid = (d.data?.pipeline_id || '').slice(0, 12)
      wsFrames.push({ recvTs: Date.now(), type: d.type, pid, latency: sendTs ? Date.now() - sendTs : null })
    } catch {}
  })
  await client.on('Network.webSocketFrameSent', (e: any) => {
    try {
      const d = JSON.parse(e.response.payloadData)
      wsSent.push({ sentTs: Date.now(), type: d.type, content: (d.data?.content || '').slice(0, 30), tid: d.data?.thread_id || d.thread_id })
    } catch {}
  })

  await page.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })

  // 登录
  const li = await page.locator('[data-testid="login-username-input"]').count()
  if (li > 0) {
    await page.fill('[data-testid="login-username-input"]', 'admin')
    await page.fill('[data-testid="login-password-input"]', 'admin123')
    await page.click('form button')
    await page.waitForTimeout(5000)
  }
  console.log('登录完成，等待2秒看CDP是否捕获WS帧...')
  await page.waitForTimeout(2000)
  console.log(`已捕获 WS 帧: ${wsFrames.length}（>0 表示CDP拦截生效）`)

  // 进会话
  await page.evaluate(() => {
    const btn = Array.from(document.querySelectorAll('button')).find((b) => b.textContent?.includes('新会话'))
    if (btn) (btn as HTMLElement).click()
  })
  await page.waitForTimeout(3000)

  // 发消息：用 focus + keyboard.type 正确触发 React 受控组件的 onChange
  const t0 = Date.now()
  wsFrames.length = 0
  wsSent.length = 0
  // 聚焦 textarea（用 evaluate focus 绕过可见性，再用 keyboard 输入触发真实键盘事件）
  await page.evaluate(() => {
    const ta = document.querySelector('textarea') as HTMLTextAreaElement
    if (ta) ta.focus()
  })
  await page.keyboard.type('回复两个字：你好', { delay: 30 })
  await page.waitForTimeout(200)
  // 确认 React state 更新了
  const val = await page.evaluate(() => (document.querySelector('textarea') as HTMLTextAreaElement)?.value)
  console.log(`输入后 textarea 值: "${val}"`)
  // Enter 发送
  await page.keyboard.press('Enter')
  await page.waitForTimeout(500)
  console.log(`[t=0] 发送消息`)
  console.log(`发送后500ms内发出的帧: ${wsSent.length}`)
  wsSent.slice(0, 3).forEach((s) => console.log(`  sent: type=${s.type} content="${s.content}" tid=${(s.tid||'').slice(0,12)}`))

  // 等50秒收响应
  await page.waitForTimeout(50000)
  const t1 = Date.now()
  console.log(`\n=== 结果（观察${((t1-t0)/1000).toFixed(1)}s）===`)
  console.log(`WS帧总数: ${wsFrames.length}`)

  // 我的pid
  const myPid = wsFrames.find((f) => f.type === 'stream_start' || f.type === 'new_message')?.pid
  const mine = wsFrames.filter((f) => f.pid === myPid)
  console.log(`我的pid: ${myPid}, 相关帧: ${mine.length}`)

  // 分布
  const dist: Record<string, number> = {}
  wsFrames.forEach((f) => { dist[f.pid || '(空)'] = (dist[f.pid || '(空)'] || 0) + 1 })
  console.log('\n帧分布(前8):')
  Object.entries(dist).sort((a,b)=>b[1]-a[1]).slice(0,8).forEach(([p,c]) => {
    console.log(`  ${p}: ${c}${p===myPid?' ★我的':''}`)
  })

  if (mine.length > 0) {
    console.log(`\n★ 首次响应: ${mine[0].recvTs - t0}ms (③前端收到，从发送算起)`)
    const lats = mine.filter(f=>f.latency!=null).map(f=>f.latency)
    if (lats.length) console.log(`  ②→③网络延迟: min=${Math.min(...lats)} max=${Math.max(...lats)} avg=${Math.round(lats.reduce((a,b)=>a+b,0)/lats.length)}ms`)
  } else {
    console.log('\n⚠️ 未捕获我的消息帧')
  }

  // 验证前端过滤效果：检查非活跃 pipeline 的帧占比
  const othersFrames = wsFrames.filter(f => f.pid !== myPid && f.pid)
  console.log(`\n=== 前端过滤验证 ===`)
  console.log(`别人pipeline帧数: ${othersFrames.length} / 总帧 ${wsFrames.length}`)
  console.log(`若 isPipelineRelevant 生效，这些帧应被丢弃不写 store`)
  // 统计前端控制台是否出现"自动注册幽灵管道"日志（不过滤才会出现）
  await browser.close()
}
main().catch(e => { console.error(e); process.exit(1) })
