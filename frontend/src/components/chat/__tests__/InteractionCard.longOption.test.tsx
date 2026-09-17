// @feature: FP-0.2.五 审批闭环（BUG-40 交互卡长选项完整可读） | @ci: frontend-test
/**
 * BUG-40 长选项渲染测试
 *
 * 用户报告：不是选项多，而是单个选项文本太长时交互卡展示不下。验收口径：
 * ①选项按钮内完整文本可读（不截断/不省略号/不横向溢出）；
 * ②长文本在按钮内换行，按钮高度随内容增长；
 * ③选项区总高超出时卡片内可纵向滚动。
 *
 * jsdom 无布局引擎：换行/固定高/滚动属 CSS 行为，以真实 tailwind-merge
 * 合成后的最终 className 为唯一可观察面断言（不 mock @/lib/utils）。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { InteractionCard } from '../InteractionCard'
import { cardProps, makeInteraction } from './interactionCardTestUtils'
import type { PendingInteraction } from '@/stores/interactionStore'

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
  DialogTitle: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  DialogFooter: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
}))

// 100+ 字中文长句（用户实测形态：一个选项的文本太多）
const LONG_LABEL =
  '采用渐进式迁移方案：先在灰度会话启用新的评估闸门插件并观察两周内的通过率与误报率指标，' +
  '再决定是否全量切换到新管道配置；期间保留旧闸门作为回滚预案，每日对比两侧裁决差异并向' +
  '负责人同步，确认稳定后再清理旧配置文件与相关测试数据，整个周期预计持续到下个迭代末尾。'

const LONG_DESCRIPTION =
  '该方案涉及数据库迁移脚本、插件清单变更、前端 schema 注册与回归测试四条工作线，' +
  '需要按依赖顺序串行推进，任何一步失败都要回滚到上一检查点并重新评估排期。'

describe('BUG-40 单个选项长文本完整可读', () => {
  it('100+ 字长 label 完整渲染于按钮内，按钮面无任何截断类', () => {
    render(
      <InteractionCard
        {...cardProps(makeInteraction({ options: [{ id: 'opt-long', label: LONG_LABEL }] }))}
      />,
    )
    // 完整文本节点存在（非省略号替换、非分片截断）
    expect(screen.getByText(LONG_LABEL)).toBeInTheDocument()
    const btn = screen.getByText(LONG_LABEL).closest('button')!
    // 按钮合成类不携带截断/禁止换行类（横向溢出根因即 whitespace-nowrap）
    expect(btn.className).not.toMatch(/line-clamp|truncate|whitespace-nowrap/)
  })

  it('长文本启用换行且高度随内容增长（解除 sm 档固定高，保留最小高）', () => {
    render(
      <InteractionCard
        {...cardProps(makeInteraction({ options: [{ id: 'opt-long', label: LONG_LABEL }] }))}
      />,
    )
    const btn = screen.getByText(LONG_LABEL).closest('button')!
    // 换行开启 + 长无空格 token 也可断行
    expect(btn.className).toMatch(/whitespace-normal/)
    expect(btn.className).toMatch(/break-words/)
    // 固定高解除（h-auto 覆盖 sm 的 h-8），最小高保留（单行选项视觉不变）
    expect(btn.className).toMatch(/h-auto/)
    expect(btn.className).not.toMatch(/(^|\s)h-8(\s|$)/)
    expect(btn.className).toMatch(/min-h-8/)
  })

  it('长 label 与短选项并存：短选项渲染与回调不受影响（区分度输入）', () => {
    const props = cardProps(
      makeInteraction({
        options: [
          { id: 'opt-long', label: LONG_LABEL },
          { id: 'opt-short', label: '批准' },
        ],
      }),
    )
    render(<InteractionCard {...props} />)
    expect(screen.getByText('批准')).toBeInTheDocument()
    // 长选项独占一行换行可读，短选项点击仍直接提交选择
    const shortBtn = screen.getByText('批准').closest('button')!
    shortBtn.click()
    expect(props.onRespondChoice).toHaveBeenCalledWith('opt-short')
  })

  it('选项区容器限高且可纵向滚动（总高超出时不撑破卡片/标题区）', () => {
    render(
      <InteractionCard
        {...cardProps(
          makeInteraction({
            options: [
              { id: 'opt-long', label: LONG_LABEL },
              { id: 'a', label: '方案A' },
              { id: 'b', label: '方案B' },
              { id: 'c', label: '方案C' },
            ],
          }),
        )}
      />,
    )
    const list = screen.getByTestId('options-list')
    expect(list.className).toMatch(/overflow-y-auto/)
    expect(list.className).toMatch(/max-h-/)
    // 纵向滚动（flex-col 断行方向）不改为横向裁切
    expect(list.className).not.toMatch(/overflow-x-auto|whitespace-nowrap/)
  })

  it('长 description 选项同样完整可读（解除单行截断）', () => {
    render(
      <InteractionCard
        {...cardProps(
          makeInteraction({
            options: [
              { id: 'a', label: '方案A', description: LONG_DESCRIPTION },
              { id: 'b', label: '方案B' },
            ],
          }),
        )}
      />,
    )
    expect(screen.getByText(LONG_DESCRIPTION)).toBeInTheDocument()
    const descSpan = screen.getByText(LONG_DESCRIPTION)
    expect(descSpan.className).not.toMatch(/line-clamp|truncate/)
    expect(screen.getByText('方案B')).toBeInTheDocument()
  })
})
