/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 消息内容渲染器的搜索高亮入口补测：搜索词仅含空白（truthy 但 trim 为空）时
 * 走「空查询原样返回」路径——正文不被改写、仍完整渲染。
 *
 * mock 边界：LobeChat Markdown 渲染体（重依赖链）；片段装配与分发走真实实现。
 */

import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => (
    <div data-testid="markdown-body">{content}</div>
  ),
}))
vi.mock('@/services/fileLoaderRegistry', () => ({
  markdownLinkInterceptor: () => vi.fn(),
}))

import { MessageContentRenderer } from '../MessageContentRenderer'

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('MessageContentRenderer — 搜索词为空白', () => {
  it('仅有空白的搜索词 → 正文原样渲染（不做高亮改写）', () => {
    render(
      <MessageContentRenderer
        fragments={[{ type: 'text', key: 'f1', content: '原始正文', isLast: true }]}
        searchQuery="   "
      />,
    )

    expect(screen.getByTestId('markdown-body')).toHaveTextContent('原始正文')
  })

  it('真实关键词同样不破坏正文（渲染器不改写 markdown 文本）', () => {
    render(
      <MessageContentRenderer
        fragments={[{ type: 'text', key: 'f2', content: '包含关键词的正文', isLast: true }]}
        searchQuery="关键词"
      />,
    )

    expect(screen.getByTestId('markdown-body')).toHaveTextContent('包含关键词的正文')
  })
})
