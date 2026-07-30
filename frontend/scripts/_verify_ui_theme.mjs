import { chromium } from 'playwright-core'
import { mkdirSync } from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const outDir = path.resolve(__dirname, '../../.workbuddy/screenshots')
mkdirSync(outDir, { recursive: true })

async function main() {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message))

  async function metrics() {
    return page.evaluate(() => {
      const root = document.documentElement
      const cs = getComputedStyle(root)
      const header = document.querySelector('[data-testid="app-header"]')
      const status = document.querySelector('[data-testid="status-bar"]')
      return {
        className: root.className,
        dsCanvas: cs.getPropertyValue('--ds-bg-canvas').trim(),
        dsPrimary: cs.getPropertyValue('--ds-accent-primary').trim(),
        textPrimary: cs.getPropertyValue('--text-primary').trim(),
        bgMain: cs.getPropertyValue('--bg-main').trim(),
        hasHeader: !!header,
        hasStatus: !!status,
        hasSidebar: !!document.querySelector('[data-testid="sidebar"]'),
        hasCtx: !!document.querySelector(
          '[data-testid="context-usage-indicator"],[data-testid="context-usage-invalid"]',
        ),
        titleBarH: header ? getComputedStyle(header).height : null,
        statusH: status ? getComputedStyle(status).height : null,
        bodyText: (document.body?.innerText || '').slice(0, 250).replace(/\n/g, ' | '),
      }
    })
  }

  async function forceTheme(id) {
    await page.evaluate((themeId) => {
      localStorage.setItem(
        'theme-storage',
        JSON.stringify({
          state: { currentThemeId: themeId, mode: themeId },
          version: 0,
        }),
      )
      // 确保五空间壳
      try {
        const raw = localStorage.getItem('layout-mode-storage')
        const parsed = raw ? JSON.parse(raw) : { state: {}, version: 0 }
        parsed.state = { ...(parsed.state || {}), mode: 'five-space' }
        localStorage.setItem('layout-mode-storage', JSON.stringify(parsed))
      } catch {
        localStorage.setItem(
          'layout-mode-storage',
          JSON.stringify({ state: { mode: 'five-space' }, version: 0 }),
        )
      }
    }, id)
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 })
    await page.waitForSelector('[data-testid="app-header"]', { timeout: 20000 })
    await page.waitForTimeout(1800)
  }

  await page.goto('http://localhost:5290/', {
    waitUntil: 'domcontentloaded',
    timeout: 30000,
  })
  await page.waitForSelector('[data-testid="app-header"]', { timeout: 20000 })

  await forceTheme('dark')
  const dark = await metrics()
  console.log('DARK', JSON.stringify(dark, null, 2))
  await page.screenshot({ path: path.join(outDir, 'verify-home-dark.png') })

  await forceTheme('light')
  const light = await metrics()
  console.log('LIGHT', JSON.stringify(light, null, 2))
  await page.screenshot({ path: path.join(outDir, 'verify-home-light.png') })

  await page.goto('http://localhost:5290/settings', {
    waitUntil: 'domcontentloaded',
    timeout: 30000,
  })
  await page.waitForTimeout(1500)
  console.log(
    'SETTINGS',
    (await page.evaluate(() => document.body.innerText.slice(0, 300))).replace(/\n/g, ' | '),
  )
  await page.screenshot({ path: path.join(outDir, 'verify-settings.png') })

  await page.goto('http://localhost:5290/settings/plugins', {
    waitUntil: 'domcontentloaded',
    timeout: 30000,
  })
  await page.waitForTimeout(1500)
  console.log(
    'PLUGINS',
    (await page.evaluate(() => document.body.innerText.slice(0, 300))).replace(/\n/g, ' | '),
  )
  await page.screenshot({ path: path.join(outDir, 'verify-plugins.png') })

  const checks = {
    darkShell: dark.hasHeader && dark.hasStatus,
    darkCanvas: /04060f/i.test(dark.dsCanvas),
    lightShell: light.hasHeader && light.hasStatus,
    lightClass: light.className.includes('light'),
    lightCanvas: /f4f7fb/i.test(light.dsCanvas),
    lightPrimary: /0891b2/i.test(light.dsPrimary),
    ctx: dark.hasCtx || light.hasCtx,
    title32: dark.titleBarH === '32px',
    status22: dark.statusH === '22px',
  }
  console.log('CHECKS', checks)
  const failed = Object.entries(checks).filter(([, v]) => !v)
  console.log(failed.length ? 'FAIL ' + failed.map(([k]) => k).join(',') : 'ALL_PASS')
  await browser.close()
  console.log('DONE')
}

main().catch((e) => {
  console.error('ERR', e)
  process.exit(1)
})
