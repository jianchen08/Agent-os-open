/**
 * 交互模式声明测试（widget 化 T9）
 *
 * 覆盖：声明装载/覆盖内置默认件/未知模式通用兜底+数据形状增强；
 * InteractionCard 特性驱动渲染（声明新模式零前端改动可渲染）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { InteractionCard } from '../InteractionCard'
import {
  clearInteractionModes,
  getInteractionModeDecl,
  loadInteractionModes,
  resolveInteractionFeatures,
} from '@/utils/interactionModes'
import type { PendingInteraction } from '@/stores/interactionStore'

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="markdown">{content}</div>
  ),
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

beforeEach(() => clearInteractionModes())
afterEach(() => clearInteractionModes())

describe('声明注册表', () => {
  it('tools[].ui.interaction_modes 装载与查询（未知条目/词汇外 features 过滤）', () => {
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
    const decl = getInteractionModeDecl('approval_v2')
    expect(decl?.features).toEqual(['options', 'text_input'])
    expect(getInteractionModeDecl('unknown')).toBeUndefined()
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

describe('选项缺 id 的 debug 留痕（FE13：宽松契约保留 + 违规率可统计）', () => {
  it('缺 id 选项点选：仍按 label 回退提交，并 console.debug 一次', () => {
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {})
    try {
      const props = cardProps(
        makeInteraction({ options: [{ label: '唯一方案' }] } as Partial<PendingInteraction>),
      )
      render(<InteractionCard {...props} />)
      fireEvent.click(screen.getByText('唯一方案'))
      // 行为不变：label 兜底提交
      expect(props.onRespondChoice).toHaveBeenCalledWith('唯一方案')
      // 留痕：缺 id 的违规可统计
      expect(debugSpy).toHaveBeenCalledWith(
        expect.stringContaining('交互选项缺 id'),
        '唯一方案',
      )
    } finally {
      debugSpy.mockRestore()
    }
  })

  it('带 id 选项点选：不触发 debug', () => {
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {})
    try {
      const props = cardProps(
        makeInteraction({ options: [{ id: 'opt-1', label: '方案一' }] } as Partial<PendingInteraction>),
      )
      render(<InteractionCard {...props} />)
      fireEvent.click(screen.getByText('方案一'))
      expect(props.onRespondChoice).toHaveBeenCalledWith('opt-1')
      expect(debugSpy).not.toHaveBeenCalled()
    } finally {
      debugSpy.mockRestore()
    }
  })
})
