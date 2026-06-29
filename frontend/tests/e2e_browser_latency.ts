/**
 * Playwright 端到端延迟测试：驱动真实浏览器测③④段
 *
 * 后端已确认无延迟（13ms响应），真实延迟必在前端。
 * 本脚本连真实前端 dev server + 真实后端，模拟用户完整操作：
 *   登录 → 发消息 → 捕获控制台 [WS_TRACE] 日志测③收到/④渲染延迟
 *
 * 测量：
 *   ③前端收到(onmessage)：[WS_TRACE] RECV latency
 *   ④前端渲染(flush→store)：[WS_TRACE] FLUSH render_delay
 *   + isPipelineRelevant 过滤是否生效（非活跃pipeline事件是否被丢弃）
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'fs'

const FRONTEND_URL = 'http://localhost:5289'
const USERNAME = 'admin'
const PASSWORD = 'admin123'
const TEST_PROMPT = '回复两个字：你好'
const WAIT_TIMEOUT = 60000

interface TraceEntry {
  time: number
  type: string
  content: string
}

async function main() {
  const browser = await chromium.launch({ headless: true })
  // 宽视口避免侧边栏响应式折叠（窄视口会隐藏会话列表）
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })

  // ★ 关键：用 addInitScript 在页面任何脚本前注入 WS 拦截
  // 这样 WebSocket 构造时就被包装，能拦截到所有连接的所有消息
  await context.addInitScript(() => {
    ;(window as any).__wsTrace = {
      events: [] as any[],
      myEvents: [] as any[],
      activePipeline: null as string | null,
      consoleTraces: [] as string[],
    }
    // 拦截 console.log 捕获 [WS_TRACE]/[WS_RAW]
    const origLog = console.log
    console.log = function (...args: any[]) {
      const text = args.map((a) => (typeof a === 'string' ? a : '')).join(' ')
      if (text.includes('WS_TRACE') || text.includes('WS_RAW')) {
        ;(window as any).__wsTrace.consoleTraces.push(text)
      }
      return origLog.apply(this, args)
    }
    // 包装 WebSocket 构造函数，拦截所有实例的 onmessage
    const OrigWS = (window as any).WebSocket
    function PatchedWS(url: string, ...rest: any[]) {
      const ws = new OrigWS(url, ...rest)
      const origAddEventListener = ws.addEventListener.bind(ws)
      ws.addEventListener = function (type: string, listener: any, ...r: any[]) {
        if (type === 'message') {
          const wrapped = (ev: MessageEvent) => {
            try {
              const d = JSON.parse(ev.data)
              const trace = (window as any).__wsTrace
              const sendTs = d.__send_ts ?? d.data?.__send_ts
              const pid = (d.data?.pipeline_id || '').slice(0, 12)
              const recvTs = Date.now()
              const latency = sendTs ? recvTs - sendTs : null
              trace.events.push({ recvTs, type: d.type, pid, latency })
              if (pid && pid === trace.activePipeline) {
                trace.myEvents.push({ recvTs, type: d.type, latency })
              }
            } catch {}
            return listener.call(this, ev)
          }
          return origAddEventListener(type, wrapped, ...r)
        }
        return origAddEventListener(type, listener, ...r)
      }
      return ws
    }
    PatchedWS.prototype = OrigWS.prototype
    ;(window as any).WebSocket = PatchedWS
    console.log('[WS_TRACE] 拦截器已注入(addInitScript)')
  })

  const page = await context.newPage()

  // 收集控制台 [WS_TRACE] 日志
  const traceLogs: TraceEntry[] = []
  page.on('console', (msg) => {
    const text = msg.text()
    if (text.includes('WS_TRACE') || text.includes('WS_RAW')) {
      traceLogs.push({ time: Date.now(), type: msg.type(), content: text })
    }
  })

  const errors: string[] = []
  page.on('pageerror', (err) => errors.push(err.message))

  console.log('=== 1. 打开前端 + 登录 ===')
  await page.goto(FRONTEND_URL, { waitUntil: 'networkidle', timeout: 30000 })

  // 检测是否需要登录（有登录页）
  const loginInput = await page.locator('[data-testid="login-username-input"]').count()
  if (loginInput > 0) {
    console.log('检测到登录页，执行登录...')
    await page.fill('[data-testid="login-username-input"]', USERNAME)
    await page.fill('[data-testid="login-password-input"]', PASSWORD)
    await page.click('[data-testid="login-form"] button[type="submit"], form button:has-text("登录")')
    await page.waitForTimeout(3000)
  }
  console.log('登录完成')

  console.log('\n=== 2. 选择/创建一个会话进入聊天界面 ===')
  let enteredChat = false
  // 用 DOM 原生 click 绕过 playwright 可见性/稳定性检查
  // （按钮在 DOM 可见，但可能处于动画/滚动容器内导致 click() 判定不稳定）
  const clicked = await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll('button'))
    const target = btns.find((b) => b.textContent?.includes('新会话'))
    if (target) { (target as HTMLElement).click(); return 'new' }
    return null
  })
  if (clicked) {
    await page.waitForTimeout(2500)
    console.log('已点击新会话(DOM click)')
    enteredChat = true
  }
  // fallback：点已有会话
  if (!enteredChat) {
    const clicked2 = await page.evaluate(() => {
      const spans = Array.from(document.querySelectorAll('span, div'))
      const target = spans.find((s) => s.textContent === '延迟测试' && s.children.length === 0)
      if (target) {
        // 点最近的会话容器（往上找可点击的父元素）
        let node: HTMLElement | null = target as HTMLElement
        for (let i = 0; i < 5 && node; i++) {
          if (node.onclick || node.getAttribute('role') === 'button' || node.tagName === 'BUTTON') {
            node.click(); return 'session'
          }
          node = node.parentElement
        }
        // 直接点文本元素
        ;(target as HTMLElement).click(); return 'session-text'
      }
      return null
    })
    if (clicked2) {
      await page.waitForTimeout(2500)
      console.log(`已点击会话(${clicked2})`)
      enteredChat = true
    }
  }
  console.log('进入会话:', enteredChat)

  console.log('\n=== 3. 等待 WS 连接稳定（拦截器已由 addInitScript 注入）===')
  await page.waitForTimeout(3000)
  // 验证拦截器生效：检查是否已收到事件（后端并发任务会持续推 keepalive）
  const earlyEvents = await page.evaluate(() => (window as any).__wsTrace?.events?.length || 0)
  console.log(`拦截器已捕获事件数: ${earlyEvents}（>0 表示拦截生效）`)

  console.log('=== 4. 找到输入框发消息 ===')
  // 轮询等待 textarea 出现（用 DOM 探测，避免 playwright 可见性误判）
  let found = false
  for (let i = 0; i < 20; i++) {
    const has = await page.evaluate(() => {
      const ta = document.querySelector('textarea')
      return ta ? { placeholder: ta.getAttribute('placeholder'), id: ta.id } : null
    })
    if (has) {
      console.log('找到输入框:', has)
      found = true
      break
    }
    await page.waitForTimeout(500)
  }
  if (!found) {
    console.log('未找到输入框，截图保存...')
    await page.screenshot({ path: 'tests/e2e_no_input.png' })
    console.log('页面URL:', page.url())
    writeFileSync('tests/e2e_trace_output.json', JSON.stringify({
      traceLogs, errors, wsTrace: await page.evaluate(() => (window as any).__wsTrace),
    }, null, 2))
    await browser.close()
    return
  }

  // 活跃 pipeline 不预先读取（store 未暴露到 window）。
  // 策略：发消息后从 WS 事件流推断——发消息后首个 stream_start/new_message 的 pid 即"我的 pid"。
  // 发消息前清空 trace，只记录发消息后的事件
  await page.evaluate(() => {
    (window as any).__wsTrace.events = []
    ;(window as any).__wsTrace.myEvents = []
  })

  console.log(`\n=== 6. 发送消息："${TEST_PROMPT}" ===`)
  const t0 = Date.now()
  // 用 DOM 原生方式填入并触发 React 事件
  await page.evaluate((prompt) => {
    const ta = document.querySelector('textarea') as HTMLTextAreaElement
    if (!ta) return
    // React 受控组件需要用原生 setter + input 事件
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!
    nativeInputValueSetter.call(ta, prompt)
    ta.dispatchEvent(new Event('input', { bubbles: true }))
  }, TEST_PROMPT)
  await page.waitForTimeout(300)
  // 按 Enter 发送（React onKeyDown）
  await page.keyboard.press('Enter')
  await page.waitForTimeout(500)
  // 也尝试原生点击发送按钮
  await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll('button'))
    const send = btns.find((b) => /发送|send|提交/i.test(b.textContent || '') && b.closest('form'))
    if (send) (send as HTMLElement).click()
  })

  console.log('等待响应（最多60秒）...')
  // 等待 AI 回复出现（消息列表里出现 assistant 内容）
  await page.waitForTimeout(WAIT_TIMEOUT).catch(() => {})

  const t1 = Date.now()
  console.log(`\n=== 7. 收集结果（观察 ${((t1 - t0) / 1000).toFixed(1)}s）===`)

  const wsTrace = await page.evaluate(() => (window as any).__wsTrace)
  console.log(`\nWS 收到事件总数: ${wsTrace.events.length}`)

  // 从事件流推断"我的 pid"：发消息后首个 stream_start/new_message 的 pid
  const myPid = (wsTrace.events as any[]).find(
    (e) => e.type === 'stream_start' || e.type === 'new_message',
  )?.pid || null
  const myEvents = (wsTrace.events as any[]).filter((e) => e.pid === myPid)
  console.log(`推断我的 pipeline: ${myPid}，相关事件: ${myEvents.length}`)

  // 按 pipeline 分布
  const pidCounts: Record<string, number> = {}
  for (const e of wsTrace.events) {
    pidCounts[e.pid || '(空)'] = (pidCounts[e.pid || '(空)'] || 0) + 1
  }
  console.log('\n事件按 pipeline 分布（前8）:')
  Object.entries(pidCounts).sort((a, b) => b[1] - a[1]).slice(0, 8).forEach(([pid, cnt]) => {
    const marker = pid === myPid ? ' ★我的' : ''
    console.log(`  ${pid}: ${cnt}${marker}`)
  })

  // 我的消息延迟（③收到延迟）
  if (myEvents.length > 0) {
    const first = myEvents[0]
    const delay = first.recvTs - t0
    console.log(`\n★ 我的消息首次响应: ${delay}ms（从发送算起，含③前端收到）`)
    console.log(`  首个事件类型: ${first.type}`)
    const latencies = myEvents.filter((e: any) => e.latency != null).map((e: any) => e.latency)
    if (latencies.length) {
      console.log(`  ②→③网络延迟: min=${Math.min(...latencies)}ms max=${Math.max(...latencies)}ms avg=${Math.round(latencies.reduce((a:number,b:number)=>a+b,0)/latencies.length)}ms`)
    }
  } else {
    console.log('\n⚠️ 未捕获到我的消息事件（可能消息未发出，或推断 pid 失败）')
  }

  // ④渲染延迟：从 addInitScript 捕获的 console 日志里找 FLUSH
  const consoleTraces: string[] = wsTrace.consoleTraces || []
  const flushLogs = consoleTraces.filter((l) => l.includes('FLUSH'))
  const recvWarns = consoleTraces.filter((l) => l.includes('RECV') && l.includes('异常'))
  console.log(`\n控制台 [WS_TRACE] 日志:`)
  console.log(`  总捕获: ${consoleTraces.length} 条`)
  console.log(`  FLUSH(渲染)日志: ${flushLogs.length} 条`)
  console.log(`  RECV异常延迟: ${recvWarns.length} 条`)
  if (recvWarns.length > 0) {
    console.log('  异常RECV样例:')
    recvWarns.slice(0, 5).forEach((l) => console.log(`    ${l.slice(0, 100)}`))
  }
  if (flushLogs.length > 0) {
    console.log('  FLUSH样例:')
    flushLogs.slice(0, 5).forEach((l) => console.log(`    ${l.slice(0, 100)}`))
  }

  // 写完整日志到文件
  writeFileSync('tests/e2e_trace_output.json', JSON.stringify({
    myPid,
    observeSeconds: (t1 - t0) / 1000,
    totalEvents: wsTrace.events.length,
    myEvents: myEvents.length,
    pidCounts,
    recvWarns,
    flushLogs,
    flushLogs: flushLogs.map((l) => l.content),
    errors,
  }, null, 2))
  console.log('\n完整数据已写入 tests/e2e_trace_output.json')

  await browser.close()
}

main().catch((e) => {
  console.error('测试失败:', e)
  process.exit(1)
})
