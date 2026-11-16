/** @feature 双端优化轮R9 | @ci: frontend-test */
/**
 * MessageItem · 气泡面作用域标记测试（用户气泡链接可读性契约）
 *
 * 契约：气泡盒携带 data-bubble（user/ai），用户气泡内的 markdown 链接
 * 由此获得 CSS 作用域锚——LobeChatMarkdown.css 按 [data-bubble='user']
 * 把链接色对齐主题预算的 --bubble-link（markdown.css .md-link 同源）。
 * 无此标记时 lobehub 链接走 antd info 桥 accent 色，在用户气泡面上
 * 对比度失效（浅色 1.06:1 / 深色 1.53:1，附件引用即此形态）。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { render } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/stores/sessionStore', () => {
  const state = { activeSessionId: 'session-1' }
  const useSessionStore = Object.assign(() => state, { getState: () => state })
  return { useSessionStore }
})
vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: (sel: (s: { pendingInteractions: unknown[] }) => unknown) =>
    sel({ pendingInteractions: [] }),
}))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: [] }),
}))
vi.mock('@/services/errorReporting', async () => (await import('./helpers/messageItemMocks')).errorReportingMock())
vi.mock('@/services/api/presenterProfiles', () => ({
  usePresenterProfile: vi.fn(() => null),
}))
// markdown mock 带真实 <a>：CSS 作用域链（气泡 → lobe 容器 → a）可被断言
vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => (
    <div data-testid="default-markdown" className="lobe-chat-isolated">
      <a href="/uploads/x.txt">{content}</a>
    </div>
  ),
}))
vi.mock('@/components/chat/MessageContentRenderer', () => ({
  default: ({ fragments }: { fragments: unknown[] }) => (
    <div data-testid="fragments-render">{fragments.length} fragments</div>
  ),
}))

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'm-1',
    sessionId: 'session-1',
    role: 'assistant',
    content: '夜色沉入湖面。',
    timestamp: new Date().toISOString(),
    parts: [],
    status: 'completed',
    ...overrides,
  } as Message
}

beforeEach(() => {
  contributionRegistry.clear()
  localStorage.clear()
  vi.mocked(apiClient.get).mockResolvedValue({ data: '<html><body>card</body></html>' })
})

describe('MessageItem — 气泡面作用域标记（data-bubble）', () => {
  it('user 行气泡携带 data-bubble=user，markdown 链接落在该作用域内', () => {
    const { container } = render(
      <MessageItem message={makeMessage({ role: 'user', content: 'test-upload.txt' })} />,
    )
    const bubble = container.querySelector("[data-bubble='user']")
    expect(bubble, '用户气泡缺 data-bubble=user 标记').not.toBeNull()
    expect(bubble?.querySelector('[data-testid="default-markdown"] a')).not.toBeNull()
    expect(container.querySelector("[data-bubble='ai']")).toBeNull()
  })

  it('assistant 行气泡携带 data-bubble=ai（作用域不外溢）', () => {
    const { container } = render(<MessageItem message={makeMessage()} />)
    const bubble = container.querySelector("[data-bubble='ai']")
    expect(bubble, 'AI 气泡缺 data-bubble=ai 标记').not.toBeNull()
    expect(bubble?.textContent).toContain('夜色沉入湖面。')
    expect(container.querySelector("[data-bubble='user']")).toBeNull()
  })
})

describe('LobeChatMarkdown.css — 用户气泡链接色契约（静态文本）', () => {
  const css = readFileSync(resolve(process.cwd(), 'src', 'components', 'chat', 'LobeChatMarkdown.css'), 'utf-8')

  it('用户气泡作用域规则在场且消费 --bubble-link（可读链接色单源）', () => {
    const rule = css
      .split('\n')
      .find((line) => line.includes("[data-bubble='user'] .lobe-chat-isolated a"))
    expect(rule, '用户气泡链接作用域规则缺失').toBeTruthy()
    const blockStart = css.indexOf('{', css.indexOf(rule!))
    const blockEnd = css.indexOf('}', blockStart)
    const block = css.slice(blockStart, blockEnd)
    expect(block).toContain('var(--bubble-link')
  })

  it('antd info token 在用户气泡作用域重定义（lobehub token 消费径）', () => {
    const line = css
      .split('\n')
      .find((l) => l.includes('[data-bubble=\'user\'] .lobe-chat-isolated {'))
    expect(line, 'token 重定义块缺失').toBeTruthy()
    const blockStart = css.indexOf('{', css.indexOf(line!))
    const blockEnd = css.indexOf('}', blockStart)
    const block = css.slice(blockStart, blockEnd)
    expect(block).toContain('--ant-color-info')
    expect(block).toContain('var(--bubble-link')
  })

  it('作用域限定 data-bubble=user（AI 气泡与全局 lobe 链接不受影响）', () => {
    const rules = css
      .split('}')
      .filter((b) => b.includes('.lobe-chat-isolated a') && b.includes('color:'))
    expect(rules.length).toBeGreaterThanOrEqual(1)
    for (const r of rules) {
      expect(r, '存在未限定用户气泡的 lobe 链接色规则').toContain("[data-bubble='user']")
    }
  })
})
