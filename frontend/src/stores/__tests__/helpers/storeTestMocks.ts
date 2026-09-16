import type { Message } from '@/types/models'
/**
 * store 测试家族共享的 vi.mock 工厂。
 *
 * 这批测试文件的 mock 头曾逐文件复制（logger/apiSession/retry 三件套），
 * 是 jscpd 克隆门禁的最大重复源；工厂放本模块后各文件一行异步引用：
 *   vi.mock('@/utils/logger', async () => (await import('./helpers/storeTestMocks')).loggerMockFull())
 *
 * 每次调用返回全新 vi.fn()，跨测试互不串扰。
 */
import { vi } from 'vitest'

export function loggerMockFull() {
  return {
    loggers: {
      sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
      websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
      stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
      pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    },
    createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
  }
}

export function loggerMockSmall() {
  return {
    loggers: {
      sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
      websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    },
  }
}

export function apiSessionMockFull() {
  return {
    getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
    mergeConsecutiveAssistantMessages: (msgs: any[]) => msgs,
  }
}

export function apiSessionMockBase() {
  return {
    getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
  }
}

export function retryMockBase() {
  return {
    retry: (fn: () => any) => fn(),
    isRetryableError: vi.fn().mockReturnValue(false),
  }
}

export function retryMockFull() {
  return {
    requestWithRetry: async (fn: () => Promise<any>) => fn(),
    retry: (fn: () => any) => fn(),
    isRetryableError: vi.fn().mockReturnValue(false),
  }
}

/** 重置 pipelineMessageStore 全部状态桶（vi.resetModules 后调用），返回新 store 实例；
 * pipelineSessionMap 可选预映射（双游标/加载族把会话→管道映射前置）。 */
export async function resetPipelineStoreState(opts?: {
  pipelineSessionMap?: Record<string, string>
}) {
  const storeMod = await import('@/stores/pipelineMessageStore')
  const usePipelineMessageStore = storeMod.usePipelineMessageStore
  usePipelineMessageStore.setState({
    messagesByPipeline: {},
    pipelines: {},
    pipelineSessionMap: opts?.pipelineSessionMap ?? {},
    streamingState: {},
    activePipelineId: null,
    topCursorsByPipeline: {},
    bottomCursorsByPipeline: {},
    hasMoreOlderByPipeline: {},
    isLoadingOlderByPipeline: {},
    reconciledByPipeline: {},
  })
  return usePipelineMessageStore
}

/** 双游标/加载族共享的消息工厂：sessionId 绑定文件常量，seq 自增时间戳 */
export function makePipelineMsgFactory(sessionId: string) {
  return (id: string, seq: number, overrides: Partial<Message> = {}): Message => ({
    id,
    sessionId,
    sequence: seq,
    role: 'assistant',
    content: '',
    timestamp: new Date(Date.now() + seq * 1000).toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  }) as Message
}

/** themeStore 测试家族共用：themeService mock 覆盖面（保留 actual 其余导出） */
export async function themeServiceWiring(importOriginal: () => Promise<Record<string, unknown>>) {
  const actual = await importOriginal()
  return {
    ...actual,
    applyTheme: vi.fn(),
    applyPluginThemeVars: vi.fn(),
    clearPluginThemeVars: vi.fn(),
    derivePluginThemePreview: vi.fn(() => ({
      primary: '#111827',
      background: '#ffffff',
      surface: '#f8fafc',
      text: '#0f172a',
      accent: '#3b82f6',
    })),
    fetchDynamicThemes: vi.fn(),
  }
}

/** 静态时间戳消息工厂（合并/顺序断言族共用） */
export function makeStaticMsgFactory(sessionId: string) {
  return (id: string, overrides: Partial<Message> = {}): Message =>
    ({
      id,
      sessionId,
      sequence: 0,
      role: 'assistant',
      content: '',
      timestamp: '2026-01-01T00:00:00Z',
      parentId: null,
      status: 'completed',
      ...overrides,
    })
}

/** 流式渲染族共用：activityConverter mock（含定制 toolCallToActivity 投影） */
export function activityConverterRichMock() {
  return {
    buildDefaultActions: (_tc: any) => [{ id: 'copy_args', icon: null, label: '复制参数', type: 'copy', onClick: () => {} }],
    toolCallToActivity: (toolCall: any) => ({
      type: 'tool_call',
      id: toolCall.call_id ?? 'activity-1',
      title: toolCall.tool_name ?? 'unknown',
      toolName: toolCall.tool_name ?? 'unknown',
      status: toolCall.status ?? 'pending',
      progress: toolCall.progress,
      currentStep: toolCall.currentStep,
      durationMs: toolCall.duration_ms,
      error: toolCall.error,
      details: toolCall.result !== undefined
        ? [{ id: 'args', label: '参数', content: toolCall.tool_args ?? {}, contentType: 'json' }]
        : [],
      actions: [],
    }),
    enhanceActivityWithToolConfig: (base: Record<string, unknown>) => base,
  }
}
