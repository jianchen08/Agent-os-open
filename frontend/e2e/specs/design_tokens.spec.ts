/**
 * 设计 token 解析 e2e —— 真实浏览器验证（jsdom 无法验证的层）
 *
 * 目的：单元测试在 jsdom 跑，jsdom 不加载真 CSS 引擎，证明不了「--font-size-caption
 * 在真浏览器里解析成 0.625rem / 10px」。本 spec 用真浏览器（Playwright）验证 token
 * 变量的定义与解析值——补上 jsdom 测不到的那一层。
 *
 * 验证方式：注入一个用 var(--token) 的元素，读 getComputedStyle 的计算值。
 * （用变量直接验证，不依赖 Tailwind 类是否被按需生成。）
 *
 * 运行：需 dev server（pnpm dev，baseURL http://localhost:5188）。
 *   pnpm exec playwright test e2e/specs/design_tokens.spec.ts
 */

import { expect, test } from '@playwright/test'

test.describe('设计 token 真实浏览器解析', () => {
  test.beforeEach(async ({ page }) => {
    // CSS 变量是全局的，登录页/根路由都带；无需登录
    await page.goto('/')
  })

  /** 读取用某 CSS 变量渲染后的计算 px 值 */
  async function computedPx(page: import('@playwright/test').Page, varExpr: string): Promise<string> {
    return page.evaluate((v) => {
      const el = document.createElement('span')
      el.style.fontSize = v
      document.body.appendChild(el)
      const px = getComputedStyle(el).fontSize
      el.remove()
      return px
    }, varExpr)
  }

  test('字号 token 阶梯解析为预期 px', async ({ page }) => {
    expect(await computedPx(page, 'var(--font-size-caption)')).toBe('10px')
    expect(await computedPx(page, 'var(--font-size-label)')).toBe('11px')
    expect(await computedPx(page, 'var(--font-size-body)')).toBe('12px')
    expect(await computedPx(page, 'var(--font-size-title)')).toBe('13px')
    expect(await computedPx(page, 'var(--font-size-page-title)')).toBe('16px')
  })

  test('图标尺寸 token 阶梯解析为预期 px', async ({ page }) => {
    expect(await computedPx(page, 'var(--icon-size-xs)')).toBe('12px')
    expect(await computedPx(page, 'var(--icon-size-sm)')).toBe('14px')
    expect(await computedPx(page, 'var(--icon-size-md)')).toBe('16px')
  })

  test('token 变量在 :root 已定义（非空）', async ({ page }) => {
    const defined = await page.evaluate(() => {
      const cs = getComputedStyle(document.documentElement)
      return {
        caption: cs.getPropertyValue('--font-size-caption').trim(),
        iconMd: cs.getPropertyValue('--icon-size-md').trim(),
      }
    })
    expect(defined.caption).not.toBe('')
    expect(defined.iconMd).not.toBe('')
  })
})
