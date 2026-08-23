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

describe('M-02: handleStreamEnd - stream_end 空内容 fallback', () => {
  beforeEach(() => {
    resetAllMocks()
    // 默认：存在一条 content 为空的 streaming 占位消息（模拟空气泡场景）
    mocks.pipelineMethods.getMessages.mockReturnValue([
      { id: 'msg-1', content: '', parts: [], status: 'streaming' },
    ])
  })

  it('stream_end 携带空 parts + 空 full_content + 消息无 content 时，应追加 warning system part', () => {
    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [], full_content: '' },
      message_id: 'msg-1',
      _threadId: 'thread-1',
    })

    expect(mocks.pipelineMethods.appendPart).toHaveBeenCalledTimes(1)
    const callArgs = mocks.pipelineMethods.appendPart.mock.calls[0]
    expect(callArgs[0]).toBe('pipe-001')
    expect(callArgs[1]).toBe('msg-1')
    const part = callArgs[2]
    expect(part.type).toBe('system')
    expect(part.content).toBe('AI 回复内容为空，请重试')
    expect(part.level).toBe('warning')
  })

  it('空内容 fallback 应通过 notificationStore 通知用户', () => {
    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [], full_content: '' },
      message_id: 'msg-1',
    })

    expect(mocks.addNotification).toHaveBeenCalledTimes(1)
    const arg = mocks.addNotification.mock.calls[0][0]
    expect(arg.message).toContain('为空')
    expect(arg.priority).toBe('normal')
    expect(arg.category).toBe('alert')
  })

  it('空内容 fallback 后消息收尾且不残留空白 content（按当前契约：空 parts 数组不触发 merge 分支）', () => {
    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [], full_content: '' },
      message_id: 'msg-1',
    })

    expect(mocks.pipelineMethods.updateMessage).toHaveBeenCalled()
    const payload = mocks.pipelineMethods.updateMessage.mock.calls[0][2]
    // 当前契约：serverParts 为空数组时走 fallback（appendPart 警告 + 仅收尾状态），
    // 不用空数组显式覆盖 parts（parts 保持本地值，避免清空已有 part）
    expect(payload.parts).toBeUndefined()
    expect(payload.status).toBe('completed')
    // 关键：不应把 content 显式设置为空字符串（避免空气泡）
    expect(payload.content).toBeUndefined()
  })

  it('full_content 非空时不应触发空内容 fallback', () => {
    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [{ type: 'text', content: 'hi' }], full_content: 'hi' },
      message_id: 'msg-1',
    })

    expect(mocks.pipelineMethods.appendPart).not.toHaveBeenCalled()
    expect(mocks.addNotification).not.toHaveBeenCalled()
    // 正常路径应把 full_content 写入 content
    const payload = mocks.pipelineMethods.updateMessage.mock.calls[0][2]
    expect(payload.content).toBe('hi')
  })

  it('消息本身已有 content 时不应触发 fallback（即使 full_content 为空）', () => {
    mocks.pipelineMethods.getMessages.mockReturnValue([
      { id: 'msg-1', content: '已有内容', parts: [], status: 'streaming' },
    ])

    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [], full_content: '' },
      message_id: 'msg-1',
    })

    expect(mocks.pipelineMethods.appendPart).not.toHaveBeenCalled()
    expect(mocks.addNotification).not.toHaveBeenCalled()
  })

  it('空内容 stream_end 仍应正常终止 pipeline streaming 状态', () => {
    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [], full_content: '' },
      message_id: 'msg-1',
      _threadId: 'thread-1',
    })

    expect(mocks.pipelineMethods.stopStreaming).toHaveBeenCalledWith('pipe-001')
  })

  it('full_content 为 null（缺失）且消息无 content 时也应触发 fallback', () => {
    handleStreamEnd({
      data: { pipeline_id: 'pipe-001', parts: [] },
      message_id: 'msg-1',
    })

    expect(mocks.pipelineMethods.appendPart).toHaveBeenCalledTimes(1)
    expect(mocks.pipelineMethods.appendPart.mock.calls[0][2].content).toBe('AI 回复内容为空，请重试')
  })
})
