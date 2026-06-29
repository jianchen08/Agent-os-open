import { chromium } from 'playwright'

async function main() {
  const browser = await chromium.launch({ headless: true })
  const page = await (await browser.newContext()).newPage()
  await page.goto('http://localhost:5289', { waitUntil: 'networkidle', timeout: 30000 })

  // 登录
  const loginInput = await page.locator('[data-testid="login-username-input"]').count()
  if (loginInput > 0) {
    await page.fill('[data-testid="login-username-input"]', 'admin')
    await page.fill('[data-testid="login-password-input"]', 'admin123')
    await page.click('form button')
    await page.waitForTimeout(4000)
  }

  console.log('URL:', page.url())
  console.log('标题:', await page.title())

  const inputs = await page.locator('textarea, input[type="text"], [contenteditable="true"], [role="textbox"]').count()
  console.log('可输入元素数:', inputs)

  const btns = await page.locator('button').allTextContents()
  console.log('按钮(前12):', btns.slice(0, 12).join(' | '))

  const chatArea = await page.locator('[data-testid*="chat" i], [data-testid*="message" i], [class*="chat" i]').count()
  console.log('聊天区域元素数:', chatArea)

  // 探测 textarea 详细
  const taCount = await page.locator('textarea').count()
  console.log('textarea数:', taCount)
  if (taCount > 0) {
    const ph = await page.locator('textarea').first().getAttribute('placeholder')
    console.log('textarea placeholder:', ph)
  }

  // 探测 contenteditable / role=textbox
  const ceCount = await page.locator('[contenteditable="true"]').count()
  console.log('contenteditable数:', ceCount)
  const tbCount = await page.locator('[role="textbox"]').count()
  console.log('role=textbox数:', tbCount)

  const bodyText = await page.locator('body').innerText()
  console.log('页面文本前200字:', bodyText.slice(0, 200).replace(/\n/g, ' '))

  await page.screenshot({ path: 'tests/e2e_logged_in.png', fullPage: false })
  console.log('截图已保存: tests/e2e_logged_in.png')
  await browser.close()
}

main().catch((e) => { console.error('失败:', e); process.exit(1) })
