// @feature FP-T12 前端组件补测
/**
 * MessageContentRenderer 缺口补测：
 * - memo 比较器：各片段类型的值比较与重渲染触发（isStreaming/长度/类型/key/字段）
 * - system 片段：info/warning/error 三级样式与图标
 * - tool_call 片段：自定义渲染器、默认渲染器连接线（total=1 与首/中/尾）
 * - text 片段：自定义 renderText（isLastStreaming 传递）、搜索高亮路径（透传）
 * - 未知片段类型 → null；空片段 → null
 */
import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MessageContentRenderer } from '../MessageContentRenderer'
import type { RenderFragment } from '@/components/chat/hooks/useMessageRender'
import type { ActivityData } from '@/types/activity'
import type { SystemLevel } from '@/types/messageParts'
import type { MessageToolCall, ThinkingContent } from '@/types/models'

const probes = vi.hoisted(() => ({
  /** 所有 stub 子组件的渲染总次数（memo 命中时子树不重渲染 → 计数不变） */
  childRenders: 0,
  lobeRenders: 0,
  lastLobeProps: null as null | { content: string; isStreaming: boolean },
}))

vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: (props: { content: string; isStreaming: boolean }) => {
    probes.childRenders += 1
    probes.lobeRenders += 1
    probes.lastLobeProps = { content: props.content, isStreaming: props.isStreaming }
    return <div data-testid="lobe-md">{props.content}</div>
  },
}))
vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => {
    probes.childRenders += 1
    return <div data-testid="plain-md">{content}</div>
  },
}))
vi.mock('@/components/chat/ThinkingDisplay', () => ({
  ThinkingDisplay: ({ thinking }: { thinking: ThinkingContent }) => {
    probes.childRenders += 1
    return <div data-testid="thinking-stub">{thinking.content}</div>
  },
}))
vi.mock('@/components/chat/ActivityCard', () => ({
  default: ({ activity }: { activity: ActivityData }) => {
    probes.childRenders += 1
    return <div data-testid="activity-stub">{activity.title}</div>
  },
}))

function makeThinking(content: string, isThinking = false): ThinkingContent {
  return { content, isThinking }
}

function makeToolCall(overrides: Partial<MessageToolCall> = {}): MessageToolCall {
  return {
    call_id: 'call-1',
    tool_name: 'file_read',
    tool_args: { path: '/tmp/a.txt' },
    status: 'completed',
    duration_ms: 120,
    ...overrides,
  }
}

function makeActivity(title: string): ActivityData {
  return { type: 'tool_call', id: `act-${title}`, title, toolName: 'file_read', status: 'completed' }
}

function textFragment(content: string, key = 't1', isLast = false): RenderFragment {
  return { type: 'text', content, key, sourceId: 'msg-1', isLast }
}

function systemFragment(content: string, level: SystemLevel, key = 's1'): RenderFragment {
  return { type: 'system', content, level, notificationType: 'test', key }
}

function toolCallFragment(overrides: Partial<MessageToolCall> = {}, index = 0, total = 1): RenderFragment {
  return {
    type: 'tool_call',
    toolCall: makeToolCall(overrides),
    activity: makeActivity(`工具-${index}`),
    key: `tc-${index}`,
    index,
    total,
  }
}

function thinkingFragment(content: string, isThinking = false): RenderFragment {
  return { type: 'thinking', thinking: makeThinking(content, isThinking), key: 'th1', sourceId: 'msg-1' }
}

function renderAt(fragments: RenderFragment[], props: { isStreaming?: boolean; className?: string; searchQuery?: string } = {}) {
  return render(<MessageContentRenderer fragments={fragments} {...props} />)
}

beforeEach(() => {
  probes.childRenders = 0
  probes.lobeRenders = 0
  probes.lastLobeProps = null
})

describe('MessageContentRenderer — 片段分发', () => {
  it('空片段列表返回 null，不渲染容器', () => {
    const { container } = renderAt([])
    expect(container.querySelector('.message-content-renderer')).not.toBeInTheDocument()
  })

  it('thinking 片段转发 ThinkingDisplay 并携带思考内容', () => {
    const { container } = renderAt([thinkingFragment('推理中…', true)])
    expect(container.querySelector('[data-testid="thinking-stub"]')).toHaveTextContent('推理中…')
  })

  it('未知片段类型渲染为 null（容器存在但无子元素）', () => {
    const fragments = [{ type: 'mystery', key: 'x' } as unknown as RenderFragment]
    const { container } = renderAt(fragments)
    const renderer = container.querySelector('.message-content-renderer')
    expect(renderer).toBeInTheDocument()
    expect(renderer?.children.length).toBe(0)
  })
})

describe('MessageContentRenderer — text 片段', () => {
  it('默认走 LobeChatMarkdown，流式末尾片段 isStreaming=true，非末尾为 false', () => {
    const { rerender } = renderAt([textFragment('末尾', 't1', true)], { isStreaming: true })
    expect(probes.lastLobeProps).toEqual({ content: '末尾', isStreaming: true })

    rerender(<MessageContentRenderer fragments={[textFragment('末尾', 't1', false)]} isStreaming={true} />)
    expect(probes.lastLobeProps?.isStreaming).toBe(false)

    rerender(<MessageContentRenderer fragments={[textFragment('末尾', 't1', true)]} isStreaming={false} />)
    expect(probes.lastLobeProps?.isStreaming).toBe(false)
  })

  it('自定义 renderText 收到 (content, isLastStreaming) 并替换默认渲染', () => {
    const renderText = (content: string, streaming: boolean) => (
      <span data-testid="custom-text">{`${content}#${streaming}`}</span>
    )
    const { container, rerender } = render(
      <MessageContentRenderer
        fragments={[textFragment('正文', 't1', true)]}
        isStreaming={true}
        renderText={renderText}
      />,
    )
    expect(container.querySelector('[data-testid="custom-text"]')).toHaveTextContent('正文#true')
    expect(container.querySelector('[data-testid="lobe-md"]')).not.toBeInTheDocument()

    rerender(
      <MessageContentRenderer
        fragments={[textFragment('正文', 't1', true)]}
        isStreaming={false}
        renderText={renderText}
      />,
    )
    expect(container.querySelector('[data-testid="custom-text"]')).toHaveTextContent('正文#false')
  })

  it('searchQuery 非空白 → 内容原样透传（高亮委托给 Markdown 渲染器，不破坏格式）', () => {
    const markdown = '# 标题\n\n带 **加粗** 的关键词内容'
    renderAt([textFragment(markdown)], { searchQuery: '关键词' })
    expect(probes.lastLobeProps?.content).toBe(markdown)
  })

  it('searchQuery 为空白串 → 走无搜索路径，内容不变', () => {
    const markdown = '空白搜索词内容'
    renderAt([textFragment(markdown)], { searchQuery: '   ' })
    expect(probes.lastLobeProps?.content).toBe(markdown)
  })
})

describe('MessageContentRenderer — tool_call 片段', () => {
  it('自定义 renderToolCall 替换默认工具卡渲染', () => {
    const renderToolCall = (fragment: Extract<RenderFragment, { type: 'tool_call' }>) => (
      <span data-testid="custom-tool">{fragment.toolCall.tool_name}</span>
    )
    const { container } = render(
      <MessageContentRenderer fragments={[toolCallFragment()]} renderToolCall={renderToolCall} />,
    )
    expect(container.querySelector('[data-testid="custom-tool"]')).toHaveTextContent('file_read')
    expect(container.querySelector('[data-testid="activity-stub"]')).not.toBeInTheDocument()
  })

  it('单工具调用（total=1）不渲染连接线', () => {
    const { container } = renderAt([toolCallFragment({}, 0, 1)])
    expect(container.querySelector('[data-testid="activity-stub"]')).toBeInTheDocument()
    expect(container.querySelector('div.relative > div.absolute')).not.toBeInTheDocument()
  })

  it.each([
    { index: 0, total: 3, expected: ['top-1/2', 'bottom-0'] },
    { index: 1, total: 3, expected: ['top-0', 'bottom-0'] },
    { index: 2, total: 3, expected: ['top-0', 'bottom-1/2'] },
  ])('多工具调用连接线按位置分三段（index=$index, total=$total）', ({ index, total, expected }) => {
    const { container } = renderAt([toolCallFragment({}, index, total)])
    const connector = container.querySelector('div.relative > div.absolute')
    expect(connector).toBeInTheDocument()
    for (const token of expected) {
      expect(connector?.className).toContain(token)
    }
  })
})

describe('MessageContentRenderer — system 片段', () => {
  it.each(['info', 'warning', 'error'] as const)('%s 级系统通知渲染内容、图标与级别样式', (level) => {
    const { container } = renderAt([systemFragment('系统消息', level)])
    const span = container.querySelector('.message-content-renderer span')
    expect(span).toHaveTextContent('系统消息')
    expect(span?.className).toContain(`text-status-${level}`)
    // 级别图标为 svg 元素
    expect(container.querySelector('.message-content-renderer svg')).toBeInTheDocument()
  })
})

describe('MessageContentRenderer — memo 比较器', () => {
  /**
   * memo 命中 → 子组件不重渲染 → childRenders 不变；
   * 比较器返回 false → 重渲染 → childRenders 增加。
   * system 片段自身无子组件，用「system + 探针 text」混合片段：
   * 探针 text 的 LobeChatMarkdown 是否重渲染反映比较器结果。
   */
  function rerenderWith(
    initial: RenderFragment[],
    next: RenderFragment[],
    props: Record<string, unknown> = {},
  ) {
    const view = renderAt(initial, props)
    const before = probes.childRenders
    view.rerender(<MessageContentRenderer fragments={next} {...props} />)
    return { before, after: probes.childRenders }
  }

  it('元素级相同（不同对象引用）→ memo 命中不重渲染', () => {
    const view = renderAt([textFragment('相同内容', 't1', false)], { className: 'x', searchQuery: 'q' })
    expect(probes.childRenders).toBe(1)
    view.rerender(
      <MessageContentRenderer
        fragments={[textFragment('相同内容', 't1', false)]}
        className="x"
        searchQuery="q"
      />,
    )
    expect(probes.childRenders).toBe(1)
  })

  it('isStreaming 变化 → 必定重渲染', () => {
    const fragments = [textFragment('流式', 't1', false)]
    const view = renderAt(fragments, { isStreaming: false })
    expect(probes.childRenders).toBe(1)
    view.rerender(<MessageContentRenderer fragments={fragments} isStreaming={true} />)
    expect(probes.childRenders).toBe(2)
  })

  it('两侧均处于流式中 → 跳过比较直接重渲染', () => {
    const fragments = [textFragment('流式', 't1', false)]
    const view = renderAt(fragments, { isStreaming: true })
    expect(probes.childRenders).toBe(1)
    view.rerender(<MessageContentRenderer fragments={fragments} isStreaming={true} />)
    expect(probes.childRenders).toBe(2)
  })

  it('片段数量变化 → 重渲染', () => {
    const { before, after } = rerenderWith(
      [textFragment('一', 't1')],
      [textFragment('一', 't1'), thinkingFragment('二')],
    )
    expect(before).toBe(1)
    // 重渲染：既有 text 重跑（+1）+ 新增 thinking 首次挂载（+1）
    expect(after).toBe(3)
  })

  it('同位置 key 变化（内容相同）→ 重渲染', () => {
    const { before, after } = rerenderWith([textFragment('内容', 't1')], [textFragment('内容', 't2')])
    expect(before).toBe(1)
    expect(after).toBe(2)
  })

  it('同位置类型变化 → 重渲染', () => {
    const { before, after } = rerenderWith([textFragment('内容', 't1')], [thinkingFragment('内容')])
    expect(before).toBe(1)
    expect(after).toBe(2)
  })

  it('text 内容变化 → 重渲染', () => {
    const { before, after } = rerenderWith([textFragment('旧', 't1')], [textFragment('新', 't1')])
    expect(before).toBe(1)
    expect(after).toBe(2)
  })

  it.each([
    { field: 'status', patch: { status: 'failed' as const } },
    { field: 'result', patch: { result: '输出内容' } },
    { field: 'error', patch: { error: '执行失败' } },
    { field: 'duration_ms', patch: { duration_ms: 999 } },
  ])('tool_call.$field 变化 → 重渲染；四字段均不变 → memo 命中', ({ patch }) => {
    const changed = rerenderWith([toolCallFragment()], [toolCallFragment(patch)])
    expect(changed.before).toBe(1)
    expect(changed.after).toBe(2)

    const base = toolCallFragment()
    const equal = rerenderWith([base], [{ ...base, activity: makeActivity('另一引用但值同') }])
    expect(equal.after).toBe(equal.before)
  })

  it.each([
    { field: 'content', next: thinkingFragment('变了') },
    { field: 'isThinking', next: thinkingFragment('思考内容', true) },
  ])('thinking.$field 变化 → 重渲染；均不变 → memo 命中', ({ next }) => {
    const changed = rerenderWith([thinkingFragment('思考内容')], [next])
    expect(changed.before).toBe(1)
    expect(changed.after).toBe(2)

    const equal = rerenderWith([thinkingFragment('思考内容')], [thinkingFragment('思考内容')])
    expect(equal.after).toBe(equal.before)
  })

  it.each([
    { field: 'content', next: systemFragment('内容变了', 'info') },
    { field: 'level', next: systemFragment('系统消息', 'error') },
  ])('system.$field 变化 → 重渲染；均不变 → memo 命中', ({ next }) => {
    // 探针：尾随 text 片段的 LobeChatMarkdown 是否重渲染反映比较器结果
    const probe = textFragment('探针', 'probe')
    const changed = rerenderWith([systemFragment('系统消息', 'info'), probe], [next, probe])
    expect(changed.before).toBe(1)
    expect(changed.after).toBe(2)

    const equal = rerenderWith(
      [systemFragment('系统消息', 'info'), probe],
      [systemFragment('系统消息', 'info'), probe],
    )
    expect(equal.after).toBe(equal.before)
  })

  it('className 变化 → 重渲染；searchQuery 变化 → 重渲染；均不变 → memo 命中', () => {
    const fragments = [textFragment('文案', 't1')]

    const view = renderAt(fragments, { className: 'a', searchQuery: 'q' })
    expect(probes.childRenders).toBe(1)
    view.rerender(<MessageContentRenderer fragments={fragments} className="b" searchQuery="q" />)
    expect(probes.childRenders).toBe(2)

    view.rerender(<MessageContentRenderer fragments={fragments} className="b" searchQuery="q2" />)
    expect(probes.childRenders).toBe(3)

    view.rerender(<MessageContentRenderer fragments={fragments} className="b" searchQuery="q2" />)
    expect(probes.childRenders).toBe(3)
  })
})
