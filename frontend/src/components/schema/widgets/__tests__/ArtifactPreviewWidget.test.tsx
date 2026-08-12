/**
 * ArtifactPreviewWidget 渲染路由测试
 *
 * 背景：此前 code 类型用纯 <pre>（不高亮）、document 类型当纯文本（不渲染 markdown）。
 * 本次复用现有渲染组件：code → CodeBlock（语法高亮）、document → MarkdownRenderer
 * （streamdown markdown）、data → CodeBlock(language='json')。
 *
 * 验证（可观察行为，mock 复用目标以断言被调用）：
 * - AC-1: code 类型 → CodeBlock 以 { code: content, language } 调用并渲染
 * - AC-2: document 类型 → MarkdownRenderer 以 { content } 调用并渲染
 * - AC-3: image 类型 → 仍走 <img>（不回归）
 * - AC-4: data 类型 → CodeBlock 以 { code: content, language: 'json' } 调用
 */
import { render, screen } from '@testing-library/react'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

// ── Mock 复用目标（断言「被调用」即可，不验证其内部高亮实现）──
vi.mock('@/components/chat/markdown', () => ({
  CodeBlock: vi.fn((props: { code?: string; language?: string }) => (
    <div data-testid="codeblock-mock" data-language={props.language ?? ''}>
      {props.code}
    </div>
  )),
  MarkdownRenderer: vi.fn((props: { content?: string }) => (
    <div data-testid="markdown-mock">{props.content}</div>
  )),
}))

import { CodeBlock, MarkdownRenderer } from '@/components/chat/markdown'
import { ArtifactPreviewWidget } from '../ArtifactPreviewWidget'

const CodeBlockMock = CodeBlock as unknown as Mock
const MarkdownRendererMock = MarkdownRenderer as unknown as Mock

describe('ArtifactPreviewWidget — 复用渲染组件', () => {
  it('AC-1: code 类型 → CodeBlock 以 { code, language } 调用并渲染', () => {
    CodeBlockMock.mockClear()
    render(
      <ArtifactPreviewWidget
        artifact={{ id: 'a1', type: 'code', language: 'python', content: 'print(1)' }}
      />,
    )

    // 被调用
    expect(CodeBlockMock).toHaveBeenCalled()
    // 以 content/language 调用
    const callProps = CodeBlockMock.mock.calls.at(-1)?.[0] as
      | { code?: string; language?: string }
      | undefined
    expect(callProps).toMatchObject({ code: 'print(1)', language: 'python' })
    // 实际渲染出来
    const mockEl = screen.getByTestId('codeblock-mock')
    expect(mockEl).toHaveTextContent('print(1)')
    expect(mockEl.getAttribute('data-language')).toBe('python')
  })

  it('AC-2: document 类型 → MarkdownRenderer 以 { content } 调用并渲染', () => {
    MarkdownRendererMock.mockClear()
    render(
      <ArtifactPreviewWidget
        artifact={{ id: 'a2', type: 'document', content: '# 标题\n正文' }}
      />,
    )

    expect(MarkdownRendererMock).toHaveBeenCalled()
    const callProps = MarkdownRendererMock.mock.calls.at(-1)?.[0] as
      | { content?: string }
      | undefined
    expect(callProps).toMatchObject({ content: '# 标题\n正文' })
    expect(screen.getByTestId('markdown-mock')).toHaveTextContent('# 标题')
  })

  it('AC-3: image 类型 → 仍走 <img>（不回归）', () => {
    render(
      <ArtifactPreviewWidget
        artifact={{
          id: 'a3',
          type: 'image',
          content: 'https://example.com/a.png',
          title: '示意图',
        }}
      />,
    )

    const img = screen.getByRole('img') as HTMLImageElement
    expect(img.src).toBe('https://example.com/a.png')
    expect(img.alt).toBe('示意图')
    // 不应触发 code/markdown 渲染路径
    expect(screen.queryByTestId('codeblock-mock')).toBeNull()
    expect(screen.queryByTestId('markdown-mock')).toBeNull()
  })

  it('AC-4: data 类型 → CodeBlock 以 { code, language: "json" } 调用', () => {
    CodeBlockMock.mockClear()
    render(
      <ArtifactPreviewWidget
        artifact={{ id: 'a4', type: 'data', content: '{"k":1}' }}
      />,
    )

    expect(CodeBlockMock).toHaveBeenCalled()
    const callProps = CodeBlockMock.mock.calls.at(-1)?.[0] as
      | { code?: string; language?: string }
      | undefined
    expect(callProps).toMatchObject({ code: '{"k":1}', language: 'json' })
  })
})
