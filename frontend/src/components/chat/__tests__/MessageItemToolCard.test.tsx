/**
 * 工具卡片（tool card）UI 优化测试
 *
 * 验证 AC-工具卡片UI-1 ~ AC-工具卡片UI-4：
 * 1. 工具卡片宽度对齐气泡宽度（不超出气泡）
 * 2. 长参数（输入/输出结果）自动换行显示
 * 3. 超长结果固定高度 + 滚动条浏览
 * 4. 工具出错时不自动展开（默认折叠，点击才展开）
 *
 * 测试策略：
 * - 渲染 MessageItem 的 isTool 分支（role === 'tool'）
 * - 断言 DOM 类名（jsdom 无 CSS 引擎，宽度/换行/滚动类名即渲染契约）
 * - 断言展开/折叠的可见性行为（可观察交互）
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

// ============================================================
// Mock 外部依赖（MessageItem 依赖较重，仅保留被测分支需要的最小依赖）
// ============================================================

vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: () => ({ activeSessionId: 'session-1' }),
}))

vi.mock('@/stores/agentStore', () => ({
  useAgentStore: () => ({ agents: [] }),
}))

vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: () => ({ pendingInteractions: [] }),
}))

vi.mock('@/utils/toolCardRegistry', () => ({
  safeParseResult: (result: unknown) =>
    result !== null && typeof result === 'object'
      ? (result as Record<string, unknown>)
      : null,
}))

vi.mock('@/services/errorReporting', () => ({
  ErrorType: { CLIENT: 'client' },
  reportError: vi.fn(),
}))

vi.mock('@/services/attachmentOpener', () => ({
  openAttachment: vi.fn(),
}))

vi.mock('@/components/chat/MessageActions', () => ({
  MessageActions: () => null,
}))

vi.mock('@/components/chat/MessageContentRenderer', () => ({
  default: () => null,
}))

vi.mock('@/components/chat/hooks/useMessageRender', () => {
  const useMessageRender = () => ({ fragments: [], isStreaming: false })
  return { useMessageRender, default: useMessageRender }
})

// ============================================================
// 测试数据工厂
// ============================================================

function makeToolMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'tool-1',
    sessionId: 'session-1',
    sequence: 2,
    role: 'tool',
    content: '',
    timestamp: new Date().toISOString(),
    status: 'completed',
    toolName: 'search',
    ...overrides,
  }
}

// ============================================================
// 测试套件
// ============================================================

describe('AC-工具卡片UI: 工具卡片 UI 优化', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  // ----------------------------------------------------------
  // AC1: 宽度对齐气泡
  // ----------------------------------------------------------
  describe('AC1: 宽度对齐气泡', () => {
    it('工具卡片容器应含气泡同宽约束类（max-w-[calc(100%-44px)]），不超出气泡', () => {
      render(
        <MessageItem
          message={makeToolMessage({ toolResult: '结果' })}
        />,
      )

      const card = screen.getByTestId('message-item')
      expect(card).toBeInTheDocument()
      expect(card.getAttribute('data-role')).toBe('tool')

      // 气泡宽度约束：与 assistant 气泡一致（max-w-[calc(100%-44px)]）
      expect(card.className).toContain('max-w-[calc(100%-44px)]')
    })
  })

  // ----------------------------------------------------------
  // AC2: 长文本自动换行
  // ----------------------------------------------------------
  describe('AC2: 长参数/结果自动换行', () => {
    it('结果容器应含语义换行类（whitespace-pre-wrap/break-words），不含 break-all（防中文每字一行）与 truncate', () => {
      const longResult = 'x'.repeat(2000)
      render(
        <MessageItem
          message={makeToolMessage({ toolResult: longResult })}
        />,
      )

      // 默认折叠，先展开
      fireEvent.click(screen.getByTestId('tool-card-toggle'))

      const resultArea = document.querySelector('[data-testid="tool-card-body"]')
      expect(resultArea).toBeInTheDocument()

      // 必须含换行类（长字符串不横向溢出）
      expect(resultArea!.className).toContain('whitespace-pre-wrap')
      // break-words（overflow-wrap: break-word）：中文按语义换行，仅超长单词/URL 断词
      expect(resultArea!.className).toContain('break-words')
      // 禁止 break-all（word-break: break-all 对中文强制每字符断行 → 每字一行）
      expect(resultArea!.className).not.toContain('break-all')
      // 禁止 truncate（nowrap 单行截断会阻止换行）
      expect(resultArea!.className).not.toContain('truncate')
    })
  })

  // ----------------------------------------------------------
  // AC3: 超长结果滚动
  // ----------------------------------------------------------
  describe('AC3: 超长结果滚动浏览', () => {
    it('结果容器应含固定最大高度 + overflow-y-auto 滚动条', () => {
      render(
        <MessageItem
          message={makeToolMessage({ toolResult: 'y'.repeat(5000) })}
        />,
      )

      fireEvent.click(screen.getByTestId('tool-card-toggle'))

      const body = document.querySelector('[data-testid="tool-card-body"]')
      expect(body).toBeInTheDocument()
      // 固定最大高度（max-h-*）
      expect(body!.className).toMatch(/max-h-/)
      // 纵向滚动
      expect(body!.className).toContain('overflow-y-auto')
    })
  })

  // ----------------------------------------------------------
  // AC4: 出错不自动展开
  // ----------------------------------------------------------
  describe('AC4: 出错不自动展开（默认折叠）', () => {
    it('含错误信息的工具卡片默认折叠，错误内容不可见；点击展开后才可见', () => {
      render(
        <MessageItem
          message={makeToolMessage({
            status: 'failed',
            toolError: '连接超时：上游服务不可达',
            toolResult: undefined,
          })}
        />,
      )

      // 默认折叠：错误内容不可见
      expect(screen.queryByText(/连接超时/)).not.toBeInTheDocument()

      // 点击展开按钮 → 错误可见
      fireEvent.click(screen.getByTestId('tool-card-toggle'))
      expect(screen.getByText(/连接超时/)).toBeInTheDocument()

      // 再次点击折叠 → 错误隐藏
      fireEvent.click(screen.getByTestId('tool-card-toggle'))
      expect(screen.queryByText(/连接超时/)).not.toBeInTheDocument()
    })

    it('含错误的工具卡片应有可点击的展开按钮（默认显示折叠态箭头）', () => {
      render(
        <MessageItem
          message={makeToolMessage({ status: 'failed', toolError: '错误' })}
        />,
      )

      const toggle = screen.getByTestId('tool-card-toggle')
      expect(toggle).toBeInTheDocument()
      expect(toggle.tagName).toBe('BUTTON')
      expect(toggle.getAttribute('aria-expanded')).toBe('false')

      fireEvent.click(toggle)
      expect(toggle.getAttribute('aria-expanded')).toBe('true')
    })

    it('成功但结果超长的工具卡片同样默认折叠（统一折叠行为）', () => {
      render(
        <MessageItem
          message={makeToolMessage({ toolResult: '正常结果' })}
        />,
      )

      // 默认折叠：结果内容不可见
      expect(screen.queryByText(/正常结果/)).not.toBeInTheDocument()

      fireEvent.click(screen.getByTestId('tool-card-toggle'))
      expect(screen.getByText(/正常结果/)).toBeInTheDocument()
    })
  })
})
