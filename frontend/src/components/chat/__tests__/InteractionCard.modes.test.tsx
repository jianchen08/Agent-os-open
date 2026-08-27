/** @feature FP-0.2.四/五 fallback-audit FE项 交互选项缺id提交留痕 @ci frontend-test */
/**
 * 交互模式声明测试（widget 化 T9）
 *
 * 覆盖：声明装载/覆盖内置默认件/未知模式通用兜底+数据形状增强；
 * InteractionCard 特性驱动渲染（声明新模式零前端改动可渲染）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { InteractionCard } from '../InteractionCard'
import { loadInteractionModes, resolveInteractionFeatures } from '@/utils/interactionModes'
import type { PendingInteraction } from '@/stores/interactionStore'

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="markdown">{content}</div>
  ),
}))

// 详情弹窗薄壳 mock：跳过 radix Dialog 的 jsdom 不兼容面（portal/动画），
// 只保留「open 时渲染内容」的行为断言所需
vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ open, children }: { open?: boolean; children?: React.ReactNode }) =>
    open ? <div data-testid="dialog-root">{children}</div> : null,
  DialogContent: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  DialogFooter: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
}))

function makeInteraction(overrides: Partial<PendingInteraction> = {}): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'choice',
    title: '交互',
    description: '',
    threadId: 'th-1',
    tabId: 'tb-1',
    agentId: 'ag-1',
    timestamp: new Date().toISOString(),
    status: 'pending',
    ...overrides,
  }
}

const cardProps = (interaction: PendingInteraction) => ({
  interaction,
  onRespondChoice: vi.fn(),
  onRespondText: vi.fn(),
  onNavigateToTab: vi.fn(),
  onDismiss: vi.fn(),
  isSubmitting: false,
})

// 注册表清理（clearInteractionModes 已随死代码删除，loadInteractionModes 幂等清空重装）
beforeEach(() => loadInteractionModes([]))

describe('声明注册表', () => {
  it('tools[].ui.interaction_modes 装载（未知条目/词汇外 features 过滤），声明覆盖内置默认件', () => {
    loadInteractionModes([
      {
        ui: {
          interaction_modes: [
            { mode: 'approval_v2', features: ['options', 'bogus_feature', 'text_input'] },
            { features: ['options'] }, // 无 mode → 丢弃
            'garbage',
          ],
        },
      },
      { ui: {} },
      {},
    ])
    const decl = resolveInteractionFeatures(makeInteraction({ mode: 'approval_v2' }))
    expect([...decl].sort()).toEqual(['options', 'text_input'].sort())
    // 未声明的未知模式 → 通用兜底（message + text_input）
    expect([...resolveInteractionFeatures(makeInteraction({ mode: 'unknown' }))].sort()).toEqual(
      ['message', 'text_input'].sort(),
    )
  })

  it('内置默认件兜底（声明缺席时三模式布局不变）', () => {
    expect([...resolveInteractionFeatures(makeInteraction({ mode: 'choice' }))].sort()).toEqual(
      ['options', 'options_detail', 'text_input'].sort(),
    )
    expect([...resolveInteractionFeatures(makeInteraction({ mode: 'conversation' }))].sort()).toEqual(
      ['navigate', 'options', 'suggestions', 'text_input'].sort(),
    )
    expect([...resolveInteractionFeatures(makeInteraction({ mode: 'notification' }))].sort()).toEqual(
      ['message', 'progress'].sort(),
    )
  })

  it('未知未声明模式：通用兜底 + 数据形状增强（带 options/progress 载荷自动补）', () => {
    const features = resolveInteractionFeatures(
      makeInteraction({
        mode: 'brand_new_mode',
        options: [{ id: 'a', label: 'A' }],
        progress: 40,
      }),
    )
    expect(features.has('options')).toBe(true)
    expect(features.has('progress')).toBe(true)
    expect(features.has('text_input')).toBe(true)
  })

  it('声明覆盖内置默认件（choice 去掉 text_input）', () => {
    loadInteractionModes([
      { ui: { interaction_modes: [{ mode: 'choice', features: ['options'] }] } },
    ])
    const features = resolveInteractionFeatures(makeInteraction({ mode: 'choice' }))
    expect(features.has('options')).toBe(true)
    expect(features.has('text_input')).toBe(false)
  })
})

describe('InteractionCard 特性驱动渲染', () => {
  it('声明的新模式零前端改动可渲染（options + text_input）', () => {
    loadInteractionModes([
      { ui: { interaction_modes: [{ mode: 'approval_v2', features: ['options', 'text_input'] }] } },
    ])
    render(
      <InteractionCard
        {...cardProps(
          makeInteraction({
            mode: 'approval_v2',
            options: [
              { id: 'ok', label: '批准' },
              { id: 'no', label: '驳回' },
            ],
          }),
        )}
      />,
    )
    expect(screen.getByText('批准')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('输入回复...')).toBeInTheDocument()
  })

  it('声明覆盖：choice 无 text_input 特性 → 输入框不渲染', () => {
    loadInteractionModes([
      { ui: { interaction_modes: [{ mode: 'choice', features: ['options'] }] } },
    ])
    render(
      <InteractionCard
        {...cardProps(
          makeInteraction({ options: [{ id: 'a', label: '选项A' }] }),
        )}
      />,
    )
    expect(screen.getByText('选项A')).toBeInTheDocument()
    expect(screen.queryByPlaceholderText('输入回复...')).not.toBeInTheDocument()
  })

  it('notification 默认：message 特性渲染 markdown 一次、无输入框/选项', () => {
    render(
      <InteractionCard
        {...cardProps(
          makeInteraction({ mode: 'notification', initialMessage: '## 报告', progress: 66 }),
        )}
      />,
    )
    expect(screen.getAllByTestId('markdown')).toHaveLength(1) // 修复前的双重渲染已除
    expect(screen.queryByPlaceholderText('输入回复...')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '进入对话' })).not.toBeInTheDocument()
  })

  it('conversation 默认：suggestions（无 options 时）+ navigate + 输入', () => {
    render(
      <InteractionCard
        {...cardProps(
          makeInteraction({ mode: 'conversation', suggestions: ['继续', '停止'] }),
        )}
      />,
    )
    expect(screen.getByText('继续')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /进入对话/ })).toBeInTheDocument()
    expect(screen.getByPlaceholderText('输入回复...')).toBeInTheDocument()
  })

  it('choice 默认：长描述选项（≥20字符）点击弹详情而非直接回调', () => {
    const props = cardProps(
      makeInteraction({
        options: [
          {
            id: 'a',
            label: '方案A',
            description: '这是一个非常长的描述文案用于触发详情弹窗逻辑分支的行为验证',
          },
        ],
      }),
    )
    render(<InteractionCard {...props} />)
    fireEvent.click(screen.getByText('方案A'))
    // 长描述 → 弹窗（不直接回调）
    expect(props.onRespondChoice).not.toHaveBeenCalled()
    expect(screen.getByText('确认选择')).toBeInTheDocument()
  })
})

describe('选项缺 id 的 fail-closed 处置（FE13 改判：人工确认核心流程不猜测提交）', () => {
  it('缺 id 选项渲染为禁用，点击不触发 onRespondChoice（label 回退已废除）', () => {
    const props = cardProps(
      makeInteraction({ options: [{ label: '唯一方案' }] } as unknown as PendingInteraction),
    )
    render(<InteractionCard {...props} />)
    const button = screen.getByText('唯一方案').closest('button')!
    // 缺 id → 禁用（fail-closed），用户可感知而非静默选错
    expect(button).toBeDisabled()
    expect(button.getAttribute('title')).toContain('缺少 id')
    fireEvent.click(screen.getByText('唯一方案'))
    expect(props.onRespondChoice).not.toHaveBeenCalled()
  })

  it('缺 id 与带 id 选项并存：仅缺 id 的被禁用，正常项照常提交', () => {
    const props = cardProps(
      makeInteraction({
        options: [
          { label: '无id项' },
          { id: 'opt-1', label: '方案一' },
        ],
      } as unknown as PendingInteraction),
    )
    render(<InteractionCard {...props} />)
    expect(screen.getByText('无id项').closest('button')).toBeDisabled()
    fireEvent.click(screen.getByText('方案一'))
    expect(props.onRespondChoice).toHaveBeenCalledTimes(1)
    expect(props.onRespondChoice).toHaveBeenCalledWith('opt-1')
  })

  it('长描述缺 id 选项同样被禁用——无法经详情弹窗路径绕过（fail-closed 全路径收口）', () => {
    const props = cardProps(
      // choice 内置 features 含 options_detail：长描述（≥20 字符）本应先弹详情窗
      makeInteraction({
        options: [{ label: '复杂方案', description: '该方案包含多阶段执行细节，需要用户逐条确认后再继续。' }],
      } as unknown as PendingInteraction),
    )
    render(<InteractionCard {...props} />)
    const button = screen.getByText('复杂方案').closest('button')!
    expect(button).toBeDisabled()
    fireEvent.click(button)
    // 既不弹窗也不提交
    expect(screen.queryByTestId('dialog-root')).not.toBeInTheDocument()
    expect(props.onRespondChoice).not.toHaveBeenCalled()
  })
})
