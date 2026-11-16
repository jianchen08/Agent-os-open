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

describe('DeclaredWidgetLayer — grid 布局（R26 监控卡密度）', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })
  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  const Card: WidgetComponent = () => <div data-testid="stub-widget">卡</div>

  /** 注册紧凑卡型与非紧凑型各一，返回监控页同构声明 */
  function seedMixed() {
    widgetRegistry.register('status_card', Card, { name: 'status_card', supportedSpaces: ['monitoring'] })
    widgetRegistry.register('chart', Card, { name: 'chart', supportedSpaces: ['monitoring'] })
    widgetRegistry.register('table', Card, { name: 'table', supportedSpaces: ['monitoring'] })
    return [
      { id: 'cpu', type: 'status_card', space: 'monitoring' },
      { id: 'trend', type: 'chart', space: 'monitoring' },
      { id: 'rows', type: 'table', space: 'monitoring' },
    ]
  }

  it('缺省 stack：容器保持纵向流，包装层无整行类（既有消费者语义不变）', () => {
    const declarations = seedMixed()
    const { container } = render(
      <DeclaredWidgetLayer declarations={declarations} space="monitoring" />,
    )

    const layer = container.querySelector('[data-testid="declared-widget-layer"]') as HTMLElement
    expect(layer.className).toContain('flex')
    expect(layer.className).toContain('flex-col')
    expect(layer.className).not.toContain('grid')
    for (const id of ['cpu', 'trend', 'rows']) {
      expect(screen.getByTestId(`declared-widget-${id}`).className).not.toContain('col-span-full')
    }
  })

  it('layout=grid：容器为 auto-fill 网格，紧凑卡型进格不整行', () => {
    const declarations = seedMixed()
    const { container } = render(
      <DeclaredWidgetLayer declarations={declarations} space="monitoring" layout="grid" />,
    )

    const layer = container.querySelector('[data-testid="declared-widget-layer"]') as HTMLElement
    expect(layer.className).toContain('grid')
    expect(layer.className).toContain('grid-cols-[repeat(auto-fill,minmax(230px,1fr))]')
    // 整行提升变体在容器上（RefreshBox 裸 div 隔层时 :has 打到真正网格子项）
    expect(layer.className).toContain('[&>*:has([data-grid-span=full])]:col-span-full')
    expect(layer.className).not.toContain('flex-col')
    // 容器宽自适应列数（容器查询语义），断言不绑定视口断点类
    expect(layer.className).not.toMatch(/\b(sm|md|lg|xl):/)
    for (const id of ['cpu', 'trend']) {
      const el = screen.getByTestId(`declared-widget-${id}`)
      expect(el.className).not.toContain('col-span-full')
      expect(el.getAttribute('data-grid-span')).toBeNull()
    }
  })

  it.each(['table', 'form'])('layout=grid：非紧凑类型 %s 整行铺满（最小可读宽度 fail-safe）', (type) => {
    widgetRegistry.register(type, Card, { name: type, supportedSpaces: ['monitoring'] })
    const { container } = render(
      <DeclaredWidgetLayer
        declarations={[{ id: 'wide', type, space: 'monitoring' }]}
        space="monitoring"
        layout="grid"
      />,
    )

    expect(container.querySelector('[data-testid="declared-widget-layer"]')).not.toBeNull()
    const wide = screen.getByTestId('declared-widget-wide')
    expect(wide.className).toContain('col-span-full')
    // 带刷新声明的声明隔 RefreshBox 裸 div，:has 变体靠该标记提升网格子项
    expect(wide.getAttribute('data-grid-span')).toBe('full')
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
