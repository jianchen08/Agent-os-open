/** @feature 双端优化轮 R22 | @ci: frontend-test */
/**
 * InteractionCard — WebView 110（Chromium <111）防回归测试（R12 审计 P2 清理）
 *
 * 待交互态边框：裸 var 基色 + alpha 修饰的两段式产物在 110 门外整条规则丢失，
 * 边框退化为 preflight 实色（40% 淡框 → 实色过强）。
 * 契约：待交互边框一律 rgb(var(--badge-info-text-rgb,fallback)/alpha) 原生 alpha
 * 语法（Chromium 65+，无门、双端一致）；-rgb 三元组由主题引擎在 badge.info
 * 变体在场时同步发射（见 themeService.rgbMirrors 测试），缺席走类内数值
 * fallback（与静态状态 info 色同族）。
 */

import { render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { InteractionCard } from '../InteractionCard'
import { cardProps, makeInteraction } from './interactionCardTestUtils'

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="markdown">{content}</div>
  ),
}))

// 详情弹窗薄壳 mock：跳过 radix Dialog 的 jsdom 不兼容面（同 modes 测试约定）
vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ open, children }: { open?: boolean; children?: React.ReactNode }) =>
    open ? <div data-testid="dialog-root">{children}</div> : null,
  DialogContent: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  DialogFooter: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
}))

describe('InteractionCard · WebView 110 兼容（待交互边框禁入 color-mix 两段式）', () => {
  it('待交互态边框为 rgb(var(--badge-info-text-rgb)/alpha) 原生 alpha 类', () => {
    const { container } = render(<InteractionCard {...cardProps(makeInteraction())} />)
    expect(container.innerHTML).toContain('rgb(var(--badge-info-text-rgb,59_130_246)/0.4)')
  })

  it('待交互态渲染产物不含任何 color-mix（110 两段式根因禁入）', () => {
    const { container } = render(<InteractionCard {...cardProps(makeInteraction())} />)
    expect(container.innerHTML).not.toContain('color-mix(')
  })
})
