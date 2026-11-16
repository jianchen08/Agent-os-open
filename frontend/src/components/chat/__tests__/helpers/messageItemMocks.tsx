/** @feature FP-0.2.四 前端Schema @vision V6 可即用 @ci frontend-test */
/**
 * MessageItem 渲染测试家族共享的 vi.mock 工厂（曾在多个测试文件逐字复制，
 * 是 jscpd 克隆门禁重复源）。统一为异步工厂一行引用：
 *   vi.mock('...', async () => (await import('./helpers/messageItemMocks')).sessionStoreActiveMock())
 * 每次调用返回全新 vi.fn()，跨测试互不串扰。
 */
import { vi } from 'vitest'

/** sessionStore：固定活跃会话 session-1（selector 感知：传选择器回所选字段，
 *  无选择器回全量 state——与 zustand 用法对齐；R5 前为恒回全量对象的平替，
 *  R5 卡片化后 MessageItem 以 selector 取 activeSessionId 作 draftKey 键域） */
export function sessionStoreActiveMock() {
  const state = { activeSessionId: 'session-1' }
  return {
    useSessionStore: (selector?: (s: typeof state) => unknown) =>
      selector ? selector(state) : state,
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

/** fileLoaderRegistry：一次性 openFileWithLoader（附件打开走加载器路由）。
 * 直链鉴权解析（useAuthedAttachmentUrl）/鉴权下载（downloadAttachment）以
 * 透传桩承担渲染（行为契约在 fileLoaderRegistry.test / AttachmentImage.test）。 */
export function attachmentOpenerMock() {
  return {
    openFileWithLoader: vi.fn(),
    markdownLinkInterceptor: () => () => false,
    useAuthedAttachmentUrl: (src?: string) =>
      src && !src.startsWith('/uploads/')
        ? { url: src, failed: false }
        : { url: 'blob:stub', failed: false },
    downloadAttachment: vi.fn(async () => undefined),
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

/** useMessageRender 桩（带文本 fragments，assistant 默认气泡路径用）。
 *  displayContent 置空：用户消息路径取 displayContent || message.content，
 *  置空强制回退真实 message.content（同一文件内 user/assistant 共桩） */
export function useMessageRenderTextMock(text: string) {
  const useMessageRender = () => ({
    fragments: [{ text }],
    isStreaming: false,
    messageId: 'assistant-1',
    displayContent: '',
  })
  return { useMessageRender, default: useMessageRender }
}

/** MessageContentRenderer 桩：fragments 文本直出（assistant 默认气泡路径用） */
export function messageContentRendererTextMock() {
  return {
    default: ({ fragments }: { fragments: Array<{ text: string }> }) => (
      <div data-testid="assistant-fragments">
        {fragments.map((f, i) => (
          <span key={i}>{f.text}</span>
        ))}
      </div>
    ),
  }
}
