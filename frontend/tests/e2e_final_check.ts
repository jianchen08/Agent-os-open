import { chromium } from 'playwright'

async function main() {
  const b = await chromium.launch({ headless: false, args: ['--start-maximized'] })
  const p = await (await b.newContext({ viewport: null })).newPage()

  // 拦截 scrollTop 赋值，记录所有设置
  await p.addInitScript(() => {
    ;(window as any).__sets = []
    const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollTop')
    if (desc && desc.set) {
      const orig = desc.set
      Object.defineProperty(HTMLElement.prototype, 'scrollTop', {
        configurable: true, get: desc.get,
        set(v: number) {
          ;(window as any).__sets.push({ t: Date.now(), v, stack: (new Error().stack||'').split('\n')[2]?.trim().slice(0,80) })
          return orig.call(this, v)
        }
      })
    }
  })

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

  // 先滚到中间（模拟用户上滑后离开的状态）
  await p.evaluate(() => {
    const ml = document.querySelector('[data-testid=message-list]') as HTMLElement
    if (ml) ml.scrollTop = ml.scrollHeight * 0.3  // 30% 位置
  })
  await p.waitForTimeout(1000)
  const beforeReload = await p.evaluate(() => {
    const ml = document.querySelector('[data-testid=message-list]') as HTMLElement
    return { st: ml?.scrollTop, max: ml ? ml.scrollHeight - ml.clientHeight : 0 }
  })
  console.log('刷新前位置（滚到30%）:', JSON.stringify(beforeReload))

  await p.evaluate(() => { ;(window as any).__sets = [] })
  console.log('\n=== 刷新 ===')
  await p.reload({ waitUntil: 'load', timeout: 60000 })

  // 等 5 秒，让一切稳定
  await p.waitForTimeout(5000)

  const final = await p.evaluate(() => {
    const ml = document.querySelector('[data-testid=message-list]') as HTMLElement
    return {
      st: ml?.scrollTop,
      max: ml ? ml.scrollHeight - ml.clientHeight : 0,
      atBottom: ml ? ml.scrollHeight - ml.scrollTop - ml.clientHeight < 80 : false,
      sets: (window as any).__sets,
    }
  })
  console.log('\n=== 5秒后最终状态 ===')
  console.log('scrollTop:', final.st, 'maxScroll:', final.max, 'atBottom:', final.atBottom)
  console.log('scrollTop 被赋值次数:', final.sets?.length)
  if (final.sets && final.sets.length > 0) {
    console.log('\n赋值记录:')
    for (const s of final.sets.slice(0, 10)) {
      console.log('  v=' + Math.round(s.v) + ' from: ' + s.stack)
    }
  }
  await b.close()
}
main().catch(e => { console.error(e); process.exit(1) })
