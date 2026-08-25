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

  test('全局滚动条可见（8px 自定义样式 + thumb 中灰高不透明度）', async ({ page }) => {
    const sb = await page.evaluate(() => {
      const outer = document.createElement('div')
      outer.style.cssText = 'width:100px;height:100px;overflow:auto'
      const inner = document.createElement('div')
      inner.style.cssText = 'width:400px;height:400px'
      outer.appendChild(inner)
      document.body.appendChild(outer)
      const result = {
        // 内容确实溢出（滚动情境成立）；headless Chromium 为 overlay 滚动条，
        // 不占布局空间，故宽高断言走伪元素计算样式而非 offsetWidth 差值
        overflowX: outer.scrollWidth > outer.clientWidth,
        overflowY: outer.scrollHeight > outer.clientHeight,
        sbWidth: getComputedStyle(outer, '::-webkit-scrollbar').width,
        sbHeight: getComputedStyle(outer, '::-webkit-scrollbar').height,
        thumbBg: getComputedStyle(outer, '::-webkit-scrollbar-thumb').backgroundColor,
      }
      outer.remove()
      return result
    })
    expect(sb.overflowX && sb.overflowY).toBeTruthy()
    expect(sb.sbWidth).toBe('8px')
    expect(sb.sbHeight).toBe('8px')
    // 性质断言：thumb 处于中灰域（80~180/255）且 alpha ≥ 0.5，
    // 保证在浅/深背景上均可见；曾因主题变量色 + 0.3 alpha 全站隐形
    const m = sb.thumbBg.match(/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+))?\s*\)/)
    expect(m, `thumbBg=${sb.thumbBg}`).not.toBeNull()
    const [r, g, b, a] = [Number(m![1]), Number(m![2]), Number(m![3]), Number(m![4] ?? 1)]
    for (const ch of [r, g, b]) {
      expect(ch).toBeGreaterThanOrEqual(80)
      expect(ch).toBeLessThanOrEqual(180)
    }
    expect(a).toBeGreaterThanOrEqual(0.5)
  })
})
