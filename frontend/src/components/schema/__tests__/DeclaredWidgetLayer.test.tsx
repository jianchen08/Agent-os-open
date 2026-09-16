/**
 * DeclaredWidgetLayer 测试 —— ui_schema 声明 widget 的渲染消费
 *
 * 意图：验证「声明 → 实现」链路在渲染侧正确落点：
 * - 已注册 type 渲染对应组件并透传 props
 * - space 过滤
 * - 未解析声明不渲染但可观测（warn）
 * - 无声明时返回 null（不污染宿主布局）
 */

import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { DeclaredWidgetLayer } from '../DeclaredWidgetLayer'
import type { WidgetComponent } from '@/services/schema/WidgetRegistry'

const Stub: WidgetComponent = (props: { label?: string }) => (
  <div data-testid="stub-widget">{props.label}</div>
)

describe('DeclaredWidgetLayer', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })
  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  it('已注册 type 渲染对应组件，透传声明 props', () => {
    widgetRegistry.register('cost_chart', Stub, { name: 'cost_chart', supportedSpaces: ['workspace'] })
    const declarations = [
      { id: 'cw1', type: 'cost_chart', space: 'workspace', props: { label: '月度成本' } },
    ]

    render(<DeclaredWidgetLayer declarations={declarations} space="workspace" />)

    expect(screen.getByTestId('declared-widget-cw1')).toBeInTheDocument()
    expect(screen.getByTestId('stub-widget')).toBeInTheDocument()
    expect(screen.getByText('月度成本')).toBeInTheDocument()
  })

  it('space 过滤：仅渲染匹配空间的声明', () => {
    widgetRegistry.register('a', Stub, { name: 'a', supportedSpaces: ['workspace'] })
    widgetRegistry.register('b', Stub, { name: 'b', supportedSpaces: ['floating'] })
    const declarations = [
      { id: 'd1', type: 'a', space: 'workspace' },
      { id: 'd2', type: 'b', space: 'floating' },
    ]

    render(<DeclaredWidgetLayer declarations={declarations} space="floating" />)

    expect(screen.queryByTestId('declared-widget-d1')).not.toBeInTheDocument()
    expect(screen.getByTestId('declared-widget-d2')).toBeInTheDocument()
  })

  it('未解析声明不渲染，但 console.warn 可观测', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const declarations = [{ id: 'broken', type: 'no_such_type', space: 'workspace' }]

    const { container } = render(<DeclaredWidgetLayer declarations={declarations} space="workspace" />)

    expect(container.querySelector('[data-testid="declared-widget-layer"]')).toBeNull()
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('1 个 widget 声明未解析'),
      expect.arrayContaining([expect.objectContaining({ id: 'broken' })]),
    )
  })

  it('无声明返回 null（不渲染容器，不污染宿主）', () => {
    const { container } = render(<DeclaredWidgetLayer declarations={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('缺省 declarations 时读 contributionRegistry.getAllWidgets（生产消费链路）', () => {
    widgetRegistry.register('chart', Stub, { name: 'chart', supportedSpaces: ['workspace'] })
    contributionRegistry.loadFromSchema({
      agents: [
        {
          id: 'cost-agent',
          name: 'Cost',
          version: '1',
          ui_schema: { widgets: [{ id: 'cw', type: 'chart', space: 'workspace' }] },
        },
      ],
    } as never)

    render(<DeclaredWidgetLayer space="workspace" />)

    expect(screen.getByTestId('declared-widget-cw')).toBeInTheDocument()
  })

  // ── 槽位语义：前端默认件 + 插件声明可覆盖 ──

  const DefaultStub: WidgetComponent = (props: { label?: string }) => (
    <div data-testid="slot-default">{props.label ?? 'default'}</div>
  )

  it('槽位无覆盖声明时渲染前端默认组件（fallbackProps 注入）', () => {
    widgetRegistry.register('chart', Stub, { name: 'chart', supportedSpaces: ['workspace'] })
    // 空间内有无关注联声明（id 不匹配槽位）
    const declarations = [
      { id: 'other', type: 'chart', space: 'chat-input', props: { label: '别的' } },
    ]

    render(
      <DeclaredWidgetLayer
        declarations={declarations}
        space="chat-input"
        slotId="voice_input"
        fallback={DefaultStub}
        fallbackProps={{ label: '前端默认语音按钮' }}
      />,
    )

    expect(screen.getByTestId('slot-default-voice_input')).toBeInTheDocument()
    expect(screen.getByText('前端默认语音按钮')).toBeInTheDocument()
    expect(screen.queryByTestId('declared-widget-other')).not.toBeInTheDocument()
  })

  it('插件声明 id === slotId 时覆盖默认组件（声明 props 生效）', () => {
    widgetRegistry.register('chart', Stub, { name: 'chart', supportedSpaces: ['workspace'] })
    const declarations = [
      { id: 'voice_input', type: 'chart', space: 'chat-input', props: { label: '插件接管' } },
    ]

    render(
      <DeclaredWidgetLayer
        declarations={declarations}
        space="chat-input"
        slotId="voice_input"
        fallback={DefaultStub}
      />,
    )

    expect(screen.getByTestId('declared-widget-voice_input')).toBeInTheDocument()
    expect(screen.getByText('插件接管')).toBeInTheDocument()
    expect(screen.queryByTestId('slot-default-voice_input')).not.toBeInTheDocument()
  })

  it('多插件声明同槽位时按 order 小者胜', () => {
    widgetRegistry.register('chart', Stub, { name: 'chart', supportedSpaces: ['workspace'] })
    const declarations = [
      { id: 'voice_input', type: 'chart', space: 'chat-input', props: { label: '后装' }, order: 20 },
      { id: 'voice_input', type: 'chart', space: 'chat-input', props: { label: '优先' }, order: 10 },
    ]

    render(
      <DeclaredWidgetLayer
        declarations={declarations}
        space="chat-input"
        slotId="voice_input"
        fallback={DefaultStub}
      />,
    )

    expect(screen.getByText('优先')).toBeInTheDocument()
    expect(screen.queryByText('后装')).not.toBeInTheDocument()
  })

  it('excludeIds 防重复：附加式层排除已被槽位消费的声明', () => {
    widgetRegistry.register('chart', Stub, { name: 'chart', supportedSpaces: ['workspace'] })
    const declarations = [
      { id: 'voice_input', type: 'chart', space: 'chat-input', props: { label: '槽位件' } },
      { id: 'addon', type: 'chart', space: 'chat-input', props: { label: '附加件' } },
    ]

    render(
      <DeclaredWidgetLayer
        declarations={declarations}
        space="chat-input"
        excludeIds={['voice_input']}
      />,
    )

    expect(screen.getByTestId('declared-widget-addon')).toBeInTheDocument()
    expect(screen.queryByTestId('declared-widget-voice_input')).not.toBeInTheDocument()
  })
})

describe('DeclaredWidgetLayer — select-option 选项追加合并', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })
  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  /** 渲染首字段 options 供断言的捕获桩（模拟 compact select 的选项源） */
  const OptionsCapture: WidgetComponent = (props: { fields?: Array<{ options?: unknown }> }) => (
    <div data-testid="options-capture">{JSON.stringify(props.fields?.[0]?.options ?? [])}</div>
  )

  const taskModeForm = {
    id: 'task_mode',
    type: 'form',
    space: 'chat-input',
    props: {
      fields: [{ name: 'mode', type: 'select', label: '任务模式', options: [{ label: '自动', value: '', icon: '✨' }] }],
    },
  }
  const writingOption = {
    id: 'mode_opt_writing',
    type: 'select-option',
    space: 'chat-input',
    order: 10,
    props: { target: 'task_mode', value: 'writing', label: '写作', icon: '✍️' },
  }

  it('select-option 并入 target 指向的 form 首字段 options（追加在兜底档之后）', () => {
    widgetRegistry.register('form', OptionsCapture, { name: 'form', supportedSpaces: ['chat-input'] })
    render(<DeclaredWidgetLayer declarations={[taskModeForm, writingOption]} space="chat-input" />)

    const merged = JSON.parse(screen.getByTestId('options-capture').textContent ?? '[]')
    expect(merged).toEqual([
      { label: '自动', value: '', icon: '✨' },
      { label: '写作', value: 'writing', icon: '✍️' },
    ])
  })

  it('select-option 不是独立 widget：被合并消费，不产生自身渲染输出', () => {
    widgetRegistry.register('form', OptionsCapture, { name: 'form', supportedSpaces: ['chat-input'] })
    render(<DeclaredWidgetLayer declarations={[taskModeForm, writingOption]} space="chat-input" />)

    expect(screen.queryByTestId('declared-widget-mode_opt_writing')).not.toBeInTheDocument()
    expect(screen.getByTestId('declared-widget-task_mode')).toBeInTheDocument()
  })

  it('target 不匹配的选项不并入；槽位式渲染（slotId）同样吃到合并结果', () => {
    widgetRegistry.register('form', OptionsCapture, { name: 'form', supportedSpaces: ['chat-input'] })
    const foreignOption = {
      id: 'mode_opt_other',
      type: 'select-option',
      space: 'chat-input',
      props: { target: 'another_selector', value: 'x', label: '别处' },
    }
    render(
      <DeclaredWidgetLayer
        declarations={[taskModeForm, writingOption, foreignOption]}
        space="chat-input"
        slotId="task_mode"
      />,
    )

    const merged = JSON.parse(screen.getByTestId('options-capture').textContent ?? '[]')
    expect(merged.map((o: { label: string }) => o.label)).toEqual(['自动', '写作'])
  })

  it('同 value 去重：本体/先声明者胜（order 小者优先保留）', () => {
    widgetRegistry.register('form', OptionsCapture, { name: 'form', supportedSpaces: ['chat-input'] })
    const dupFirst = {
      id: 'opt_dup_a',
      type: 'select-option',
      space: 'chat-input',
      order: 5,
      props: { target: 'task_mode', value: 'writing', label: '写作甲' },
    }
    const dupSecond = {
      id: 'opt_dup_b',
      type: 'select-option',
      space: 'chat-input',
      order: 9,
      props: { target: 'task_mode', value: 'writing', label: '写作乙' },
    }
    render(<DeclaredWidgetLayer declarations={[taskModeForm, dupFirst, dupSecond]} space="chat-input" />)

    const merged = JSON.parse(screen.getByTestId('options-capture').textContent ?? '[]')
    expect(merged.filter((o: { value: string }) => o.value === 'writing')).toHaveLength(1)
    expect(merged[1].label).toBe('写作甲')
  })
})
