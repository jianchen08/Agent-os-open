/**
 * 精确抓"刷新后跳到底部"的触发源（headful 模式）
 *
 * 拦截 scrollTop 的所有赋值，记录调用栈 + 时刻。
 * 同时高频采样（10ms）记录 scrollTop 变化轨迹。
 */
import { chromium } from 'playwright'

async function main() {
  const browser = await chromium.launch({
    headless: false,  // 有头模式，模拟真实浏览器
    args: ['--start-maximized'],
  })
  const context = await browser.newContext({ viewport: null })
  const page = await context.newPage()

  // 注入 scrollTop 拦截器（在任何脚本前）
  await context.addInitScript(() => {
    ;(window as any).__scrollLog = [] as any[]
    ;(window as any).__sampling = [] as any[]
    ;(window as any).__consoleLog = [] as any[]
    // 拦截 HTMLElement.scrollTop 的 set
    const proto = HTMLElement.prototype
    const desc = Object.getOwnPropertyDescriptor(proto, 'scrollTop')
    if (desc && desc.set) {
      const origSet = desc.set
      Object.defineProperty(proto, 'scrollTop', {
        ...desc,
        set(v: number) {
          ;(window as any).__scrollLog.push({
            t: Date.now(),
            value: v,
            stack: new Error().stack?.split('\n').slice(1, 8)
              .map((s) => s.trim().replace(/at |@/g, '').slice(0, 60))
              .join(' | '),
          })
          return origSet.call(this, v)
        },
      })
    }
    // 拦截 console 捕获 effect 相关日志
    const origLog = console.log
    const origDebug = console.debug
    console.log = function (...args: any[]) {
      const t = args.map((a) => (typeof a === 'string' ? a : '')).join(' ')
      if (t.includes('钉底') || t.includes('首次') || t.includes('EFFECT') || t.includes('initFromAPI') || t.includes('定位')) {
        ;(window as any).__consoleLog.push(t.slice(0, 150))
      }
      return origLog.apply(this, args)
    }
    console.debug = console.log
    console.log('[拦截器] 已 hook')
  })

  await page.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })
  await page.fill('[data-testid="login-username-input"]', 'admin')
  await page.fill('[data-testid="login-password-input"]', 'admin123')
  await page.click('form button')
  await page.waitForTimeout(5000)

  // 进入修仙会话
  await page.evaluate(() => {
    const spans = Array.from(document.querySelectorAll('span, div, button'))
    const t = spans.find((s) => (s.textContent || '').includes('真实修仙游戏') && s.children.length === 0)
    if (t) (t as HTMLElement).click()
  })
  await page.waitForTimeout(5000)

  // 清空日志，刷新
  await page.evaluate(() => { (window as any).__scrollLog = [] })
  console.log('=== 刷新（headful，抓 scrollTop 赋值轨迹）===')
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 })

  // 高频采样 6 秒
  await page.evaluate(() => {
    return new Promise<void>((resolve) => {
      const t0 = Date.now()
      const iv = setInterval(() => {
        const scroll = document.querySelector('[data-testid="message-list"]') as HTMLElement
        ;(window as any).__sampling.push({
          t: Date.now() - t0,
          scrollTop: scroll?.scrollTop ?? -1,
          scrollHeight: scroll?.scrollHeight ?? -1,
        })
        if (Date.now() - t0 > 6000) { clearInterval(iv); resolve() }
      }, 20)
    })
  })

  const data = await page.evaluate(() => ({
    scrollLog: (window as any).__scrollLog,
    sampling: (window as any).__sampling,
    consoleLog: (window as any).__consoleLog,
  }))

  // 分析采样：找跳变点
  console.log('\n=== scrollTop 采样轨迹（变化时打印）===')
  let prev: any = null
  for (const s of data.sampling) {
    if (!prev || Math.abs(s.scrollTop - prev.scrollTop) > 2 || Math.abs(s.scrollHeight - prev.scrollHeight) > 2) {
      const ch = s.scrollHeight - (prev?.scrollHeight || 0)
      const mark = s.scrollHeight - s.scrollTop < 600 ? ' ✅底' : (s.scrollTop > 0 ? ' ⚠️中' : '')
      console.log(`  t=${String(s.t).padStart(5)} top=${String(s.scrollTop).padStart(6)} scrollH=${String(s.scrollHeight).padStart(6)} ΔH=${ch > 0 ? '+' : ''}${ch}${mark}`)
    }
    prev = s
  }

  // 分析 scrollTop 赋值日志
  console.log(`\n=== scrollTop 赋值次数: ${data.scrollLog.length} ===`)
  // 聚类：按调用栈分组
  const groups: Record<string, number> = {}
  for (const l of data.scrollLog) {
    const key = l.stack?.slice(0, 80) || '(无栈)'
    groups[key] = (groups[key] || 0) + 1
  }
  console.log('\n按调用栈分组（前8）:')
  Object.entries(groups).sort((a, b) => b[1] - a[1]).slice(0, 8).forEach(([stack, cnt]) => {
    console.log(`  [${cnt}次] ${stack}`)
  })

  // 找最大的几次跳变（scrollTop 大幅增加 = 跳向底部）
  const jumps = data.scrollLog
    .map((l: any, i: number) => ({ ...l, delta: l.value - (data.scrollLog[i - 1]?.value || 0) }))
    .filter((l: any) => Math.abs(l.delta) > 100)
  if (jumps.length) {
    console.log(`\n=== 大幅跳变（|Δ|>100）: ${jumps.length} 次 ===`)
    for (const j of jumps.slice(0, 8)) {
      console.log(`  t=${j.t} Δ=${j.delta > 0 ? '+' : ''}${j.delta}`)
      console.log(`    栈: ${j.stack}`)
    }
  }

  console.log(`\n=== effect/console 日志 (${data.consoleLog.length} 条) ===`)
  for (const l of data.consoleLog.slice(0, 15)) {
    console.log(`  ${l}`)
  }

  await browser.close()
}

main().catch((e) => { console.error(e); process.exit(1) })
