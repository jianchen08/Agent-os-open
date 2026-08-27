/**
 * M-01 / M-02 修复端到端行为验证（回归测试）
 *
 * 从用户视角验证两个 Must Fix 的真实行为：
 * - M-01: 通用 ERROR 事件 handler（handleGlobalError）
 *         收到 error 事件 → 解析错误 → notificationStore 通知 → 终止 streaming
 * - M-02: stream_end 空内容 fallback
 *         收到空内容 stream_end → 追加 warning system part → 通知用户 → 不出现空白气泡
 *
 * 通过 mock zustand store，直接调用 handler 函数，断言 store 调用，
 * 模拟"后端发事件 → 前端 handler 处理 → 用户看到通知/消息变化"的真实链路。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

// ── 用 vi.hoisted 定义所有 mock 函数，确保 vi.mock 工厂能安全引用 ──
const mocks = vi.hoisted(() => {
  const pipelineMethods = {
    getMessages: vi.fn(() => [] as any[]),
    appendPart: vi.fn(),
    updateMessage: vi.fn(),
    updatePart: vi.fn(),
    finalizeMessage: vi.fn(),
    stopStreaming: vi.fn(),
    removeMessage: vi.fn(),
    registerPipeline: vi.fn(),
    findStreamingPartIndex: vi.fn(() => -1),
    appendToPart: vi.fn(),
    activePipelineId: null as string | null,
    // handleStreamEnd → pipelineRegistryStore.applyStreamStatus 反查归属会话时读取：
    // 新管道不在 registry.runs 中时用 pipelineSessionMap / pipelines 反查（F3 修复：
    // 此前缺这两个字段，applyStreamStatus 内 pipelineSessionMap[pipelineId] 抛 TypeError）
    pipelineSessionMap: {} as Record<string, string>,
    pipelines: {} as Record<string, { sessionId?: string }>,
  }
  return {
    pipelineMethods,
    addNotification: vi.fn(() => 'notif-id'),
    updateUsage: vi.fn(),
    autoRenameSessionIfNeeded: vi.fn(),
  }
})

// ── Mock 所有 handler 依赖的 store / logger ──
vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: { getState: () => mocks.pipelineMethods },
}))
vi.mock('@/stores/notificationStore', () => ({
  useNotificationStore: { getState: () => ({ addNotification: mocks.addNotification }) },
}))
vi.mock('@/stores/contextUsageStore', () => ({
  useContextUsageStore: { getState: () => ({ updateUsage: mocks.updateUsage }) },
}))
vi.mock('@/stores/sessionListStore', () => ({
  useSessionListStore: { getState: () => ({ autoRenameSessionIfNeeded: mocks.autoRenameSessionIfNeeded }) },
}))
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: { getState: () => ({ activeSessionId: 'session-x' }) },
}))
vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: { getState: () => ({ getActiveTab: () => null }) },
}))
vi.mock('@/utils/logger', () => ({
  loggers: { websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } },
}))

// ── 导入被测 handler（真实 router.ts / utils.ts，依赖已被 mock）──
import { handleGlobalError, handleStreamEnd } from '../streaming/handlers'

// ── 重置辅助 ──
function resetAllMocks(): void {
  mocks.pipelineMethods.getMessages.mockReturnValue([])
  mocks.pipelineMethods.activePipelineId = null
  mocks.pipelineMethods.appendPart.mockClear()
  mocks.pipelineMethods.updateMessage.mockClear()
  mocks.pipelineMethods.updatePart.mockClear()
  mocks.pipelineMethods.finalizeMessage.mockClear()
  mocks.pipelineMethods.stopStreaming.mockClear()
  mocks.pipelineMethods.removeMessage.mockClear()
  mocks.pipelineMethods.registerPipeline.mockClear()
  mocks.addNotification.mockClear()
  mocks.updateUsage.mockClear()
  mocks.autoRenameSessionIfNeeded.mockClear()
}

// ════════════════════════════════════════════════════════════════════
// M-01: handleGlobalError —— 通用 ERROR 事件 handler
// ════════════════════════════════════════════════════════════════════
// M-01（handleGlobalError）已随 ADR 2026-08-21 死代码清理删除：
// 后端（kernel ws_session/capability_router/插件 event-bus）无 'error' 事件发射源，
// handler 与订阅均为零消费者死代码；缺 pipelineId 按 threadId 清管道的兜底属
// 「清别人状态」反模式，一并废除。

describe('M-02: handleStreamEnd - 空轮次收尾（逐轮模型）', () => {
  beforeEach(() => {
    resetAllMocks()
    // 默认：存在一条 content 为空的 streaming 占位消息（init/exit 等无产出轮次）
    mocks.pipelineMethods.getMessages.mockReturnValue([
      { id: 'msg-1', content: '', parts: [], status: 'streaming' },
    ])
  })

  it('空轮次 stream_end（无 new_message 无内容）→ 静默移除占位，不追加警示卡/不弹通知', () => {
    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [], full_content: '' },
      message_id: 'msg-1',
      _threadId: 'thread-1',
    })

    expect(mocks.pipelineMethods.removeMessage).toHaveBeenCalledWith('pipe-001', 'msg-1')
    expect(mocks.pipelineMethods.appendPart).not.toHaveBeenCalled()
    expect(mocks.addNotification).not.toHaveBeenCalled()
  })

  it('空轮次收尾仍正常终止 pipeline streaming 状态', () => {
    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [], full_content: '' },
      message_id: 'msg-1',
      _threadId: 'thread-1',
    })

    expect(mocks.pipelineMethods.stopStreaming).toHaveBeenCalledWith('pipe-001')
  })

  it('full_content 为 null（缺失）且消息无内容时同样移除占位', () => {
    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [] },
      message_id: 'msg-1',
    })

    expect(mocks.pipelineMethods.removeMessage).toHaveBeenCalledWith('pipe-001', 'msg-1')
    expect(mocks.addNotification).not.toHaveBeenCalled()
  })

  it('full_content 非空时走正常收尾，不误删消息', () => {
    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [{ type: 'text', content: 'hi' }], full_content: 'hi' },
      message_id: 'msg-1',
    })

    expect(mocks.pipelineMethods.removeMessage).not.toHaveBeenCalled()
    expect(mocks.pipelineMethods.appendPart).not.toHaveBeenCalled()
    expect(mocks.addNotification).not.toHaveBeenCalled()
    // 正常路径应把 full_content 写入 content
    const payload = mocks.pipelineMethods.updateMessage.mock.calls[0][2]
    expect(payload.content).toBe('hi')
  })

  it('消息本身已有 content 时不应移除（即使 full_content 为空）', () => {
    mocks.pipelineMethods.getMessages.mockReturnValue([
      { id: 'msg-1', content: '已有内容', parts: [], status: 'streaming' },
    ])

    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [], full_content: '' },
      message_id: 'msg-1',
    })

    expect(mocks.pipelineMethods.removeMessage).not.toHaveBeenCalled()
    expect(mocks.pipelineMethods.appendPart).not.toHaveBeenCalled()
    expect(mocks.addNotification).not.toHaveBeenCalled()
  })
})
