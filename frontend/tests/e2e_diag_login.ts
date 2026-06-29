import { chromium } from 'playwright'

async function main() {
  const b = await chromium.launch({ headless: true })
  const p = await (await b.newContext({ viewport: { width: 1600, height: 900 } })).newPage()
  await p.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })
  await p.fill('[data-testid="login-username-input"]', 'admin')
  await p.fill('[data-testid="login-password-input"]', 'admin123')
  await p.click('form button')
  await p.waitForTimeout(6000)

  const token = await p.evaluate(() => {
    return localStorage.getItem('auth_token') || localStorage.getItem('token') || localStorage.getItem('access_token') || '(无token)'
  })
  console.log('token:', String(token).slice(0, 30))
  console.log('URL:', p.url())
  const stillLogin = await p.locator('[data-testid="login-page"]').count()
  console.log('登录页仍在:', stillLogin)

  const visBtns = await p.locator('button:visible').allTextContents()
  console.log('可见按钮:', visBtns.filter((x) => x.trim()).slice(0, 15))

  // 可见的会话相关元素
  const visTexts = await p.locator('span:visible, div:visible').allTextContents()
  const sessionHits = visTexts.filter((t) => /延迟|测试|会话|新会话|追踪/.test(t)).slice(0, 8)
  console.log('会话相关可见文本:', sessionHits)

  // 尝试找到侧边栏切换按钮
  const toggleBtns = await p.locator('button:visible[aria-label], button:visible').evaluateAll((btns) =>
    btns.filter((b: any) => /menu|侧|sidebar|toggle|展开|收起/i.test(b.getAttribute('aria-label') || '') || /menu|侧|展开|收起/.test(b.textContent || ''))
      .map((b: any) => ({ label: b.getAttribute('aria-label'), text: b.textContent?.trim().slice(0, 20) }))
  )
  console.log('可能的侧边栏按钮:', toggleBtns)

  // 看 body 文本确认布局
  const t = await p.locator('body').innerText()
  console.log('可见文本前200:', t.slice(0, 200).replace(/\n/g, ' '))
  await b.close()
}
main().catch((e) => { console.error(e); process.exit(1) })
