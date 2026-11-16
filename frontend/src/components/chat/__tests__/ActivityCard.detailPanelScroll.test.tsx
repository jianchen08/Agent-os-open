/** @feature: FP-0.2.四 前端Schema(工具卡详情面板布局契约) | @ci: frontend-test */
/**
 * 功能测试：ActivityCard 展开详情面板的高度上限与内部滚动契约。
 *
 * 用户可感知的契约（断行为，不断内部实现）：
 * - 展开后的详情面板不得撑高外层对话流：面板设高度上限，内容不足时自适应收缩
 * - 超出上限的内容在面板内部纵向滚动，且滚动不外溢带动外层会话滚动
 * - 折叠态与展开/收起交互保持不变
 *
 * 为什么这样断言：限高/滚动属于纯 CSS 布局行为，jsdom 无 layout engine，
 * 无法测真实像素高度，故断言驱动该行为的类契约（项目内同类样式契约测试沿用
 * 此口径，见 ChoiceDescriptionDialog.test.tsx「弹窗内容应包含 max-h-[60vh] 样式」）。
 * 断言按语义（有限高/可纵向滚动/滚动链隔离/无固定高）而非具体数值，换一个上限
 * 档位不应误报，但"缺少任一语义"必须变红。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ActivityCard from '../ActivityCard'
import type { ActivityData } from '@/types/activity'

// jsdom 环境下 MarkdownRenderer / approval 依赖链（react-markdown、diff 渲染器）
// 不参与本文件的断言，替换为空组件以隔离渲染副作用
vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: () => null,
}))
vi.mock('@/components/approval', () => ({
  TextDiffView: () => null,
}))
vi.mock('@/utils/toolCardRegistry', () => ({
  getGlobalOpenFileCallback: () => vi.fn(),
}))
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
}))

const PANEL_TESTID = 'activity-detail-panel'

function makeActivity(overrides: Partial<ActivityData> = {}): ActivityData {
  return {
    type: 'tool_call',
    id: 'act-panel',
    title: '工具调用面板',
    status: 'completed',
    ...overrides,
  } as ActivityData
}

/** 详情块：count 个长文本块，模拟工具输出量大 */
function makeDetails(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    id: `d${i}`,
    label: `块${i}`,
    contentType: 'text' as const,
    content: 'x'.repeat(200),
  }))
}

/**
 * 面板限高契约断言：有限高 + 纵向滚动 + 滚动链隔离 + 无固定高（内容自适应）
 */
function expectPanelCapped(panel: HTMLElement) {
  const cls = panel.className
  // 高度上限（内容超出即截断，不撑高外层）
  expect(cls).toMatch(/(^|\s)max-h-/)
  // 超出部分在面板内部纵向滚动
  expect(cls).toMatch(/(^|\s)overflow-y-auto(\s|$)/)
  // 滚动链隔离：面板滚到底不外溢带动外层会话滚动
  expect(cls).toMatch(/(^|\s)overscroll-contain(\s|$)/)
  // 内容不足时自适应收缩（不得写死高度）
  expect(cls).not.toMatch(/(^|\s)h-\[/)
}

function queryPanel() {
  return screen.queryByTestId(PANEL_TESTID)
}

describe('ActivityCard 展开详情面板：限高 + 内部滚动', () => {
  describe('面板高度契约（内容多少都不撑高外层）', () => {
    // 性质断言：详情块数量跨 0/1/20 三档，面板限高契约恒成立
    it.each([0, 1, 20])('%i 个详情块 → 面板限高 + 内部滚动（内容自适应）', (count) => {
      render(<ActivityCard activity={makeActivity({ details: makeDetails(count) })} defaultExpanded />)
      const panel = queryPanel()
      expect(panel).toBeInTheDocument()
      expectPanelCapped(panel as HTMLElement)
    })

    it('大体量载荷（实时输出 + 多详情块 + 错误）→ 面板仍限高且包裹全部详情', () => {
      render(
        <ActivityCard
          activity={makeActivity({
            partialOutput: ['实时输出片段'],
            details: makeDetails(20),
            error: '工具执行失败',
          })}
          defaultExpanded
        />,
      )
      const panel = queryPanel() as HTMLElement
      expectPanelCapped(panel)
      // 详情内容确实渲染在受限面板内部（内容保留，只是不再撑高外层）
      expect(panel).toHaveTextContent('实时输出片段')
      expect(panel).toHaveTextContent('工具执行失败')
    })
  })

  describe('折叠态与展开/收起交互保持不变', () => {
    it('默认折叠 → 不渲染详情面板', () => {
      render(<ActivityCard activity={makeActivity({ details: makeDetails(3) })} />)
      expect(queryPanel()).not.toBeInTheDocument()
    })

    it('点击头部展开 → 面板出现且限高；再次点击收起 → 面板消失', () => {
      render(<ActivityCard activity={makeActivity({ details: makeDetails(3) })} />)
      expect(queryPanel()).not.toBeInTheDocument()

      fireEvent.click(screen.getByText('工具调用面板'))
      const panel = queryPanel() as HTMLElement
      expect(panel).toBeInTheDocument()
      expectPanelCapped(panel)

      fireEvent.click(screen.getByText('工具调用面板'))
      expect(queryPanel()).not.toBeInTheDocument()
    })
  })
})
