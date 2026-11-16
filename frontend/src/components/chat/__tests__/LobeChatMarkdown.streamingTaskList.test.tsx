/** @feature FP-T12 流式 task-list 组件装配 | @ci: frontend-test */
// @ci: frontend-test
/**
 * LobeChatMarkdown 流式 task-list 组件装配契约测试。
 *
 * 背景（2026-10-25 装机 #130 实证）：react-markdown v10 的组件解析按键存在性
 * 取值（hast-util-to-jsx-runtime `own.call(components, name) ? components[name]
 * : name`）——`ul: isStreaming ? undefined : TaskListGate` 的 components 对象
 * 永远带 `ul` 键，流式期间值为 undefined → 每个 ul 渲染成 undefined 组件 →
 * React #130（Element type is invalid: got undefined）整页崩到路由错误边界。
 * 契约：流式时 `ul` 键必须**整键缺席**（回退默认 ul），非流式才挂 TaskListGate。
 *
 * 桩形态：@lobehub/ui Markdown 桩把 components 原样转发给真实 ReactMarkdown
 * （linkClick 测试的旧桩丢弃 components——本契约的被测对象就是 components
 * 装配本身，必须真流）。CodeBlock/MarkdownProvider 按 ContentCardGate 契约
 * 测试同款 mock（分发契约测试，不拉 lobehub 内部实现）。
 */
import { render, screen } from '@testing-library/react'
import ReactMarkdown from 'react-markdown'
import { describe, expect, it, vi } from 'vitest'
import { LobeChatMarkdown } from '../LobeChatMarkdown'

vi.mock('@lobehub/ui', () => ({
  ConfigProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  Markdown: ({
    children,
    components,
  }: {
    children?: React.ReactNode
    components?: Record<string, unknown>
  }) => <ReactMarkdown components={components}>{children}</ReactMarkdown>,
}))
vi.mock('@lobehub/ui/es/ConfigProvider/index', () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}))
vi.mock('@lobehub/ui/es/Markdown/index', () => ({
  default: ({
    children,
    components,
  }: {
    children?: React.ReactNode
    components?: Record<string, unknown>
  }) => <ReactMarkdown components={components}>{children}</ReactMarkdown>,
}))
vi.mock('@lobehub/ui/es/Markdown/components/MarkdownProvider', () => ({
  useMarkdownContext: vi.fn(() => ({
    animated: false,
    enableHtmlPreview: true,
    enableMermaid: true,
    fullFeaturedCodeBlock: false,
  })),
}))
vi.mock('@lobehub/ui/es/Markdown/components/CodeBlock', () => ({
  CodeBlock: ({ children }: { children?: unknown }) => (
    <pre data-testid="default-codeblock">{String(children ?? '')}</pre>
  ),
}))

const TASK_LIST_MD = '执行计划：\n\n- [ ] 第一步\n- [ ] 第二步\n'

describe('LobeChatMarkdown 流式 task-list 组件装配', () => {
  it('流式中含任务清单 → 默认 ul 正常渲染，不崩（#130 回归锚）', () => {
    expect(() =>
      render(<LobeChatMarkdown content={TASK_LIST_MD} isStreaming />),
    ).not.toThrow()

    const items = document.querySelectorAll('ul li')
    expect(items.length).toBe(2)
    expect(screen.getByText(/第一步/)).toBeTruthy()
  })

  it('非流式且无声明 → 默认 ul 原样渲染', () => {
    render(<LobeChatMarkdown content={TASK_LIST_MD} />)

    const items = document.querySelectorAll('ul li')
    expect(items.length).toBe(2)
    expect(document.querySelector('[data-content-card]')).toBeNull()
  })
})
