/** @feature FP-0.2.四/五 人类交互卡描述 markdown 渲染(两处描述位统一走 MarkdownRenderer) | @ci: frontend-test */
/**
 * 交互卡描述文本 markdown 渲染（2026-11-06 用户反馈）：
 * LLM 写的 description 常带 markdown 结构（列表/加粗/代码），纯文本 <p> 渲染
 * 会让全文挤成一坨不可读——两处描述位（标题区/消息区兜底）统一走 MarkdownRenderer。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { InteractionCard } from '../InteractionCard'
import { cardProps, makeInteraction } from './interactionCardTestUtils'

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="markdown">{content}</div>
  ),
}))

const MD_DESCRIPTION = '方案要点：\n- **第一阶段**：搭建骨架\n- `npm install` 装依赖'

describe('InteractionCard 描述 markdown 渲染', () => {
  it('标题区描述走 MarkdownRenderer（choice 默认件）', () => {
    render(
      <InteractionCard
        {...cardProps(makeInteraction({ mode: 'choice', description: MD_DESCRIPTION }))}
      />,
    )
    expect(screen.getAllByTestId('markdown').length).toBeGreaterThan(0)
    expect(screen.getAllByTestId('markdown').some((el) => el.textContent === MD_DESCRIPTION)).toBe(
      true,
    )
    // 纯文本 <p> 渲染路径不得回归（描述原样直出 = 挤成一坨的旧形态）
    expect(screen.queryByText(MD_DESCRIPTION)).toBeNull()
  })

  it('message 特性无 initialMessage 时描述兜底同样走 MarkdownRenderer', () => {
    // 未知模式 → 通用兜底（message + text_input）
    render(
      <InteractionCard
        {...cardProps(makeInteraction({ mode: 'unknown', description: MD_DESCRIPTION }))}
      />,
    )
    expect(screen.getAllByTestId('markdown').length).toBeGreaterThan(0)
  })

  it('initialMessage 优先：消息区渲染 initialMessage，不重复渲染描述', () => {
    render(
      <InteractionCard
        {...cardProps(
          makeInteraction({ mode: 'unknown', description: MD_DESCRIPTION, initialMessage: '开场白' }),
        )}
      />,
    )
    expect(screen.getByText('开场白')).toBeInTheDocument()
    const markdowns = screen.getAllByTestId('markdown')
    expect(markdowns.some((el) => el.textContent === '开场白')).toBe(true)
  })
})
