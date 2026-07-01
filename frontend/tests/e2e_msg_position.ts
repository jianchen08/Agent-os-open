/**
 * 复现"刷新后消息显示位置不对"
 *
 * 对比刷新前后：
 *  - 消息数量、顺序、首尾内容
 *  - 滚动位置（scrollTop / scrollHeight）
 *  - after_sequence 断线补漏返回的内容
 *
 * 判断：是否 after_sequence 修复（只返回新 record）导致位置错乱
 */
import { chromium } from 'playwright'

const FRONTEND = 'http://localhost:5289'

async function snapshot(page: any, label: string) {
  const snap = await page.evaluate(() => {
    const msgs = Array.from(document.querySelectorAll('[data-testid="message-item"], [data-role]'))
    const scroll = document.querySelector('[data-testid="message-list"]') as HTMLElement
    return {
      msgCount: msgs.length,
      scrollTop: scroll?.scrollTop,
      scrollHeight: scroll?.scrollHeight,
      clientHeight: scroll?.clientHeight,
      atBottom: scroll ? (scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 50) : null,
    }
  })
  console.log(`[${label}] msgs=${snap.msgCount} scrollTop=${snap.scrollTop} scrollHeight=${snap.scrollHeight} atBottom=${snap.atBottom}`)
  return snap
}

async function main() {
  const browser = await chromium.launch({ headless: false })  // 有头模式便于观察
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } })

  // CDP 抓 messages 请求的响应
  const page = await context.newPage()
  const client = await context.newCDPSession(page)
  await client.send('Network.enable')
  const msgResponses: any[] = []
  await client.on('Network.responseReceived', async (e: any) => {
    if (e.response.url.includes('messages')) {
      try {
        const body = await client.send('Network.getResponseBody', { requestId: e.requestId })
        const data = JSON.parse(body.body)
        msgResponses.push({
          url: e.response.url.replace(/^https?:\/\/[^/]+/, ''),
          msgCount: data.messages?.length || 0,
          firstSeq: data.messages?.[0]?.sequence,
          lastSeq: data.messages?.[data.messages.length - 1]?.sequence,
        })
      } catch {}
    }
  })

  await page.goto(FRONTEND, { waitUntil: 'networkidle', timeout: 30000 })
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

  console.log('=== 刷新前 ===')
  await snapshot(page, '刷新前')

  // 刷新后高频采样（50ms），抓"中间→底部"跳变
  console.log('\n=== 执行刷新 + 高频采样（50ms）===')
  msgResponses.length = 0
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 })

  // 先看 persist 快照存了什么
  const persistSnap = await page.evaluate(() => {
    const raw = localStorage.getItem('pipeline-messages')
    if (!raw) return { has: false }
    try {
      const d = JSON.parse(raw)
      const d2 = d.state || d
      const pids = Object.keys(d2.messagesByPipeline || {})
      const first = pids[0] ? d2.messagesByPipeline[pids[0]] : []
      return {
        has: true,
        pipelineCount: pids.length,
        firstPidMsgs: first.length,
        firstPidFirstSeq: first[0]?.sequence,
        firstPidLastSeq: first[first.length - 1]?.sequence,
      }
    } catch (e: any) { return { has: false, err: e.message } }
  })
  console.log('\npersist 快照:', JSON.stringify(persistSnap))

  const trace = await page.evaluate(() => {
    return new Promise<any>((resolve) => {
      const samples: any[] = []
      const t0 = Date.now()
      const iv = setInterval(() => {
        const scroll = document.querySelector('[data-testid="message-list"]') as HTMLElement
        const msgs = document.querySelectorAll('[data-testid="message-item"], [data-role]').length
        const st = scroll?.scrollTop ?? -1
        const sh = scroll?.scrollHeight ?? -1
        const ch = scroll?.clientHeight ?? -1
        samples.push({ t: Date.now() - t0, scrollTop: st, scrollHeight: sh, clientHeight: ch, msgs, atBottom: sh - st - ch < 50 })
        if (samples.length >= 80) {  // 4秒
          clearInterval(iv)
          resolve(samples)
        }
      }, 50)
    })
  })

  // 找到第一个"到底部"的时刻，和之前"在中间"的对比
  console.log('\nscrollTop/scrollHeight 变化（只打印变化的采样）:')
  let prev: any = null
  for (const s of trace) {
    const changed = !prev || Math.abs(s.scrollTop - prev.scrollTop) > 1 || Math.abs(s.scrollHeight - prev.scrollHeight) > 1 || s.msgs !== prev.msgs
    if (changed) {
      const mark = s.atBottom ? ' ✅底部' : (s.scrollTop > 0 && s.scrollHeight > 0 ? ' ⚠️中间' : '')
      console.log(`  t=${String(s.t).padStart(5)} top=${String(s.scrollTop).padStart(6)} scrollH=${String(s.scrollHeight).padStart(6)} msgs=${s.msgs}${mark}`)
    }
    prev = s
  }

  console.log('\n=== messages 请求 ===')
  for (const r of msgResponses) {
    console.log(`  ${r.url.slice(0, 75)}`)
    console.log(`    返回 ${r.msgCount} 条, seq ${r.firstSeq}-${r.lastSeq}`)
  }

  await browser.close()
}

main().catch((e) => { console.error(e); process.exit(1) })
