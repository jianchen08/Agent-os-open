/** @feature FP-0.2.四 前端Schema @vision V6 可即用 @ci frontend-test */
/**
 * MessageItem 渲染测试家族共享的 vi.mock 工厂（曾在多个测试文件逐字复制，
 * 是 jscpd 克隆门禁重复源）。统一为异步工厂一行引用：
 *   vi.mock('...', async () => (await import('./helpers/messageItemMocks')).sessionStoreActiveMock())
 * 每次调用返回全新 vi.fn()，跨测试互不串扰。
 */
import { vi } from 'vitest'

/** sessionStore：固定活跃会话 session-1 */
export function sessionStoreActiveMock() {
  return {
    useSessionStore: () => ({ activeSessionId: 'session-1' }),
  }
}

/** agentStore：空 agent 列表 */
export function agentStoreEmptyMock() {
  return {
    useAgentStore: () => ({ agents: [] }),
  }
}

/** interactionStore：无待决交互（直接返回形态） */
export function interactionStoreEmptyMock() {
  return {
    useInteractionStore: () => ({ pendingInteractions: [] }),
  }
}

/** interactionStore：无待决交互（selector 形态） */
export function interactionStoreSelectorMock() {
  return {
    useInteractionStore: (selector: (s: { pendingInteractions: unknown[] }) => unknown) =>
      selector({ pendingInteractions: [] }),
  }
}

/** errorReporting：CLIENT 错误类型 + 一次性 reportError */
export function errorReportingMock() {
  return {
    ErrorType: { CLIENT: 'client' },
    reportError: vi.fn(),
  }
}

/** attachmentOpener：一次性 openAttachment */
export function attachmentOpenerMock() {
  return {
    openAttachment: vi.fn(),
  }
}

/** MessageActions 桩（不渲染） */
export function messageActionsStubMock() {
  return {
    MessageActions: () => null,
  }
}

/** LobeChatMarkdown 轻量桩：content 直出（拉起 @lobehub/ui 全家桶在 vitest 下不可行） */
export function lobeMarkdownStubMock() {
  return {
    LobeChatMarkdown: ({ content }: { content: string }) => (
      <div data-testid="user-markdown">{content}</div>
    ),
  }
}

/** MessageContentRenderer 桩（默认导出不渲染） */
export function messageContentRendererStubMock() {
  return {
    default: () => null,
  }
}

/** useMessageRender 桩：空 fragments、非流式（default + 命名双导出） */
export function useMessageRenderStubMock() {
  const useMessageRender = () => ({ fragments: [], isStreaming: false })
  return { useMessageRender, default: useMessageRender }
}
