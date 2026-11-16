/** @feature 双端优化轮 R22 | @ci: frontend-test */
/**
 * FilePreview — WebView 110（Chromium <111）防回归测试（R12 审计 P2 清理）
 *
 * 图片预览画布 tint：Tailwind v4 对「裸 var 基色 + alpha 修饰」任意值类生成
 * 两段式产物——@supports color-mix 门内才是 30% 淡底，110 不过门 → 整条规则
 * 丢失、画布 tint 消失。
 * 契约：画布 tint 一律 rgb(var(--code-bg-rgb,fallback)/alpha) 原生 alpha 语法
 * （Chromium 65+，无门、双端一致）；-rgb 三元组由主题引擎随 --code-bg 同步发射
 * （深色 rgba 串/浅色 hex 均可解析，见 themeService.rgbMirrors 测试），
 * 类内数值 fallback 对齐静态默认底 #1A1A2E 的通道值。
 */

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { FilePreview } from '@/components/workspace/FilePreview'

function renderImagePreview() {
  return render(<FilePreview filePath="assets/photo.png" content="" containerTaskId="ct-1" />)
}

describe('FilePreview · WebView 110 兼容（图片画布 tint 禁入 color-mix 两段式）', () => {
  it('图片预览画布为 rgb(var(--code-bg-rgb)/alpha) 原生 alpha 类', () => {
    const { container } = renderImagePreview()
    expect(container.innerHTML).toContain('rgb(var(--code-bg-rgb,26_26_46)/0.3)')
  })

  it('渲染产物不含任何 color-mix（110 两段式根因禁入）', () => {
    const { container } = renderImagePreview()
    expect(container.innerHTML).not.toContain('color-mix(')
  })
})
