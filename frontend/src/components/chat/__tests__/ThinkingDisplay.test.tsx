// @feature FP-T12 前端组件补测
/**
 * ThinkingDisplay 组件测试：
 * - 头部：标题、思考中/完成图标、步骤数徽标、耗时徽标、点击展开收起
 * - 内容区：步骤列表（类型标签/状态图标/内容）、子步骤递归编号、步骤头点击折叠
 * - 流式内容：思考中走 <pre> 原文、结束后走 MarkdownRenderer；空内容占位提示
 * - 滚动贴底跟随：贴底时跟随最新内容，用户上滑暂停，滑回底部恢复
 */
import { fireEvent, render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ThinkingDisplay } from '../ThinkingDisplay'
import type { ThinkingContent, ThinkingStep } from '@/types/models'

vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="plain-md">{content}</div>
  ),
}))

function makeStep(overrides: Partial<ThinkingStep> & { id: string }): ThinkingStep {
  return {
    type: 'reasoning',
    content: `步骤内容-${overrides.id}`,
    status: 'completed',
    timestamp: '2026-09-13T00:00:00Z',
    ...overrides,
  }
}

function makeThinking(overrides: Partial<ThinkingContent>): ThinkingContent {
  return { content: '', isThinking: false, ...overrides }
}

function renderDisplay(thinking: ThinkingContent, props: { defaultExpanded?: boolean } = {}) {
  return render(<ThinkingDisplay thinking={thinking} {...props} />)
}

beforeEach(() => {
  localStorage.clear()
})

describe('ThinkingDisplay — 头部', () => {
  it('默认折叠：只渲染头部标题，内容区不存在', () => {
    const { container, getByText } = renderDisplay(makeThinking({ content: '结论' }))
    expect(getByText('思考过程')).toBeInTheDocument()
    expect(container.querySelector('.thinking-text-content')).not.toBeInTheDocument()
    // 折叠态不泄漏思考内容
    expect(container.textContent).not.toContain('结论')
  })

  it('isThinking=true 头部为旋转加载图标；结束后为完成图标', () => {
    const { container, rerender } = renderDisplay(makeThinking({ content: '', isThinking: true }))
    expect(container.querySelector('.animate-spin')).toBeInTheDocument()

    rerender(<ThinkingDisplay thinking={makeThinking({ content: '', isThinking: false })} />)
    expect(container.querySelector('.animate-spin')).not.toBeInTheDocument()
  })

  it('有步骤时显示「N 步」徽标；无步骤不显示', () => {
    const steps = [makeStep({ id: 's1' }), makeStep({ id: 's2' })]
    const { getByText, queryByText, rerender } = renderDisplay(makeThinking({ content: '', steps }))
    expect(getByText('2 步')).toBeInTheDocument()

    rerender(<ThinkingDisplay thinking={makeThinking({ content: '', steps: [] })} />)
    expect(queryByText(/步$/)).not.toBeInTheDocument()
  })

  it('durationMs 显示为秒（一位小数）；缺省不显示耗时徽标', () => {
    const { getByText, queryByText, rerender } = renderDisplay(makeThinking({ content: '', durationMs: 1523 }))
    expect(getByText('1.5s')).toBeInTheDocument()

    rerender(<ThinkingDisplay thinking={makeThinking({ content: '' })} />)
    expect(queryByText('1.5s')).not.toBeInTheDocument()
  })

  it('点击头部展开/再点击收起', () => {
    const { container, getByText } = renderDisplay(makeThinking({ content: '思考正文' }))
    fireEvent.click(getByText('思考过程'))
    expect(container.querySelector('.thinking-text-content')).toBeInTheDocument()
    expect(container.textContent).toContain('思考正文')

    fireEvent.click(getByText('思考过程'))
    expect(container.querySelector('.thinking-text-content')).not.toBeInTheDocument()
  })
})

describe('ThinkingDisplay — 步骤列表', () => {
  it('defaultExpanded 时渲染步骤列表：序号、类型标签、内容', () => {
    const steps = [
      makeStep({ id: 's1', type: 'analysis', content: '拆解问题' }),
      makeStep({ id: 's2', type: 'planning', content: '制定计划' }),
    ]
    const { getByText } = renderDisplay(makeThinking({ content: '', steps }), { defaultExpanded: true })
    expect(getByText('步骤 1')).toBeInTheDocument()
    expect(getByText('步骤 2')).toBeInTheDocument()
    expect(getByText('分析')).toBeInTheDocument()
    expect(getByText('规划')).toBeInTheDocument()
    expect(getByText('拆解问题')).toBeInTheDocument()
    expect(getByText('制定计划')).toBeInTheDocument()
  })

  it.each([
    { type: 'reasoning', label: '推理' },
    { type: 'analysis', label: '分析' },
    { type: 'planning', label: '规划' },
    { type: 'evaluation', label: '评估' },
  ])('步骤类型 $type 显示标签「$label」', ({ type, label }) => {
    const steps = [makeStep({ id: 's1', type })]
    const { getByText } = renderDisplay(makeThinking({ content: '', steps }), { defaultExpanded: true })
    expect(getByText(label)).toBeInTheDocument()
  })

  it('未知步骤类型原样展示类型键', () => {
    const steps = [makeStep({ id: 's1', type: 'reflection' as ThinkingStep['type'] })]
    const { getByText } = renderDisplay(makeThinking({ content: '', steps }), { defaultExpanded: true })
    expect(getByText('reflection')).toBeInTheDocument()
  })

  it.each([
    { status: 'pending', iconClass: 'text-status-warning', borderClass: 'border-status-warning' },
    { status: 'running', iconClass: 'text-status-info', borderClass: 'border-status-info' },
    { status: 'completed', iconClass: 'text-status-success', borderClass: 'border-status-success' },
    { status: 'failed', iconClass: 'text-status-error', borderClass: 'border-status-error' },
  ] as const)('步骤状态 $status 渲染对应状态图标与左边框色', ({ status, iconClass, borderClass }) => {
    const steps = [makeStep({ id: 's1', status })]
    const { container, getByText } = renderDisplay(makeThinking({ content: '', steps }), {
      defaultExpanded: true,
    })
    expect(getByText(`步骤内容-s1`)).toBeInTheDocument()
    expect(container.querySelector(`[class*="${iconClass}"]`)).not.toBeNull()
    expect(container.querySelector(`[class*="${borderClass}"]`)).not.toBeNull()
  })

  it('子步骤递归渲染，编号为「父.子」层级格式', () => {
    const steps = [
      makeStep({
        id: 's1',
        subSteps: [
          makeStep({ id: 's1-1', content: '子步骤一' }),
          makeStep({
            id: 's1-2',
            content: '子步骤二',
            subSteps: [makeStep({ id: 's1-2-1', content: '孙步骤' })],
          }),
        ],
      }),
    ]
    const { getByText } = renderDisplay(makeThinking({ content: '', steps }), { defaultExpanded: true })
    expect(getByText('步骤 1')).toBeInTheDocument()
    expect(getByText('步骤 1.1')).toBeInTheDocument()
    expect(getByText('步骤 1.2')).toBeInTheDocument()
    expect(getByText('步骤 1.2.1')).toBeInTheDocument()
    expect(getByText('子步骤一')).toBeInTheDocument()
    expect(getByText('孙步骤')).toBeInTheDocument()
  })

  it('点击步骤头部折叠该步骤（仅隐藏内容，其他步骤不受影响）', () => {
    const steps = [makeStep({ id: 's1' }), makeStep({ id: 's2', content: '第二步内容' })]
    const { container, getByText } = renderDisplay(makeThinking({ content: '', steps }), {
      defaultExpanded: true,
    })
    expect(getByText('步骤内容-s1')).toBeInTheDocument()

    // 步骤 1 的头部（含「步骤 1」文本的点击区）
    const stepHeader = getByText('步骤 1').closest('div.cursor-pointer') as HTMLElement
    fireEvent.click(stepHeader)
    expect(container.textContent).not.toContain('步骤内容-s1')
    expect(getByText('第二步内容')).toBeInTheDocument()
  })
})

describe('ThinkingDisplay — 流式思考内容', () => {
  it('思考中：内容以 pre 原文展示，不走 MarkdownRenderer', () => {
    const { container, queryByTestId } = renderDisplay(
      makeThinking({ content: '# 草稿 **未完成**', isThinking: true }),
      { defaultExpanded: true },
    )
    const pre = container.querySelector('pre')
    expect(pre).not.toBeNull()
    expect(pre?.textContent).toBe('# 草稿 **未完成**')
    expect(queryByTestId('plain-md')).not.toBeInTheDocument()
  })

  it('思考结束：内容交给 MarkdownRenderer 渲染', () => {
    const { container, getByTestId } = renderDisplay(
      makeThinking({ content: '# 结论 正文', isThinking: false }),
      { defaultExpanded: true },
    )
    expect(container.querySelector('pre')).toBeNull()
    expect(getByTestId('plain-md')).toHaveTextContent('# 结论 正文')
  })

  it('思考中但暂无内容：显示「正在思考中...」占位', () => {
    const { getByText, queryByTestId } = renderDisplay(
      makeThinking({ content: '', isThinking: true }),
      { defaultExpanded: true },
    )
    expect(getByText('正在思考中...')).toBeInTheDocument()
    expect(queryByTestId('plain-md')).not.toBeInTheDocument()
  })

  it('非思考且无内容：既无 pre 也无 Markdown 也无占位', () => {
    const { container, queryByTestId } = renderDisplay(
      makeThinking({ content: '', isThinking: false }),
      { defaultExpanded: true },
    )
    expect(container.querySelector('pre')).toBeNull()
    expect(queryByTestId('plain-md')).not.toBeInTheDocument()
    expect(container.textContent).not.toContain('正在思考中')
  })
})

describe('ThinkingDisplay — 滚动贴底跟随', () => {
  function mockScrollMetrics(el: HTMLElement, scrollHeight: number, clientHeight: number) {
    const recorded: number[] = []
    let scrollTop = 0
    Object.defineProperty(el, 'scrollHeight', { configurable: true, value: scrollHeight })
    Object.defineProperty(el, 'clientHeight', { configurable: true, value: clientHeight })
    Object.defineProperty(el, 'scrollTop', {
      configurable: true,
      get: () => scrollTop,
      set: (v: number) => {
        recorded.push(v)
        scrollTop = v
      },
    })
    return { recorded, setScrollTop: (v: number) => (scrollTop = v) }
  }

  function getContentEl(container: HTMLElement): HTMLElement {
    const el = container.querySelector('.thinking-text-content')
    if (!(el instanceof HTMLElement)) throw new Error('内容滚动区未渲染')
    return el
  }

  it('贴底时新内容到达自动滚动到底；用户上滑暂停跟随；滑回底部恢复跟随', () => {
    const { container, rerender } = renderDisplay(
      makeThinking({ content: '第一段', isThinking: true }),
      { defaultExpanded: true },
    )
    const el = getContentEl(container)
    const { recorded, setScrollTop } = mockScrollMetrics(el, 500, 100)

    // 用户停在距底 400px 处（上滑看历史）→ 暂停跟随：内容更新不再滚动
    fireEvent.scroll(el)
    rerender(<ThinkingDisplay thinking={makeThinking({ content: '第二段', isThinking: true })} defaultExpanded />)
    expect(recorded).toEqual([])

    // 用户滑回贴底位置（距底 < 28px）→ 恢复跟随：内容更新滚动到底（scrollTop=500）
    setScrollTop(450)
    fireEvent.scroll(el)
    rerender(<ThinkingDisplay thinking={makeThinking({ content: '第三段', isThinking: true })} defaultExpanded />)
    expect(recorded).toEqual([500])
  })

  it('挂载即展开且未上滑（跟随标记初始为真）：内容更新自动滚动到底', () => {
    const { container, rerender } = renderDisplay(
      makeThinking({ content: '首段', isThinking: true }),
      { defaultExpanded: true },
    )
    const el = getContentEl(container)
    const { recorded } = mockScrollMetrics(el, 800, 200)
    // 未发生 scroll 事件 → 贴底标记保持初始 true → 内容更新触发滚动到底
    rerender(<ThinkingDisplay thinking={makeThinking({ content: '次段', isThinking: true })} defaultExpanded />)
    expect(recorded).toEqual([800])
  })
})
