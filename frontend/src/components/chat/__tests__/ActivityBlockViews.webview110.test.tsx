/** @feature 双端优化轮 R22 | @ci: frontend-test */
/**
 * ActivityBlockViews — WebView 110（Chromium <111）防回归测试（R12 审计 P2 清理）
 *
 * 表格行底边：原类引用 --border-border——全仓无此变量定义的悬空引用，叠加
 * alpha 修饰后成两段式产物（@supports color-mix 门内 + 门外裸 var），110 不过
 * 门整条丢失、门外 fallback 亦非法（悬空 var）→ 边框退化为 currentColor 实色。
 * 契约：行底边走 border-border 语义类（hsl(var(--border)) 原生 hsl，--border
 * 为主题引擎发射的 HSL 值，可携带声明级 alpha；任何主题/引擎下与桌面同渲染，
 * 与同文件其余块视图的行边框模式一致）。带 alpha 修饰的 hsl 两段包装
 * （hsl(var(--x)/N)）对「值本身携带 alpha」的主题双 alpha 非法，禁入。
 */

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { DetailBlock } from '../ActivityBlockViews'

const tableBlock = {
  label: '数据表',
  content: '',
  contentType: 'table' as const,
  table: { columns: ['列A', '列B'], rows: [['x', 'y'], ['1', '2']] },
}

describe('ActivityBlockViews · WebView 110 兼容（表格行底边禁入 color-mix 两段式）', () => {
  it('表格行底边为 border-border 语义类（原生 hsl(var(--border))，无 alpha 修饰包装）', () => {
    const { container } = render(<DetailBlock block={tableBlock} />)
    const td = container.querySelector('td')
    expect(td).not.toBeNull()
    expect(td!.className).toContain('border-border')
    expect(td!.className).not.toContain('--border-border')
  })

  it('渲染产物不含 color-mix，悬空变量 border-border 禁入', () => {
    const { container } = render(<DetailBlock block={tableBlock} />)
    expect(container.innerHTML).not.toContain('color-mix(')
    expect(container.innerHTML).not.toContain('--border-border')
  })
})
