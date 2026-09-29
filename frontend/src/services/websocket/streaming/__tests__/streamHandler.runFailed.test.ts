// @feature FP-T12 补测 | @ci frontend-test
/**
 * handleRunFailed（run_failed 事件）行为测试——run.failed 前端镜像
 * （ADR 2026-09-28-run-failure-frontend-notification）：
 * - 署名失败/引擎失败（stop_reason 有无两形态）→ 通知中心弹「管道运行失败」卡
 * - run_id 作通知幂等键：同事件重放（断线重连重放缓冲）不重复弹卡
 * - 与聊天路径 stream_error 交叉去重：ENGINE_RUN_FAILED 双事件只弹一张卡
 *   （两顺序均成立），stream_error 的消息级失败标记不受去重影响
 * - 无 pipeline_id → 跳过不弹卡不抛错
 * - 副作用：注册表 runs 缓存标 failed + streamingState 清理
 *
 * 全部走真实 zustand store + 全局 queryClient 单例，不 mock 内部依赖。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readPipelineRuns, updatePipelineRunsCache } from '@/hooks/queries/usePipelineRunsQuery'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import type { PipelineRunInfo } from '@/types/pipeline'
import { handleRunFailed, handleStreamError } from '../handlers/streamHandler'

// 模块级失败卡去重账本跨用例存活：每用例独立管道 id 隔离（生产语义=不同管道）。
const PID_SIGNED = 'pipe-rf-signed-0000000000000000000001'
const PID_ENGINE = 'pipe-rf-engine-0000000000000000000002'
const PID_SIDEEFFECT = 'pipe-rf-sideeffect-00000000000003'
const PID_MISSING = 'pipe-rf-missi-000000000000000000004'
const PID_REPLAY = 'pipe-rf-replay-00000000000000000005'
const PID_DEDUP = 'pipe-rf-dedup-0000000000000000000006'
const PID_DEDUP_REV = 'pipe-rf-deduprev-000000000000000007'

function runFailedEvent(pipelineId: string, runId: string, stopReason: string | null) {
  return {
    data: {
      pipeline_id: pipelineId,
      _threadId: 'thread-runfailed-t',
      status: 'failed',
      stop_reason: stopReason,
      run_id: runId,
    },
  }
}

function seedRun(pipelineId: string, runId: string): void {
  const run: PipelineRunInfo = {
    run_id: runId,
    pipeline_id: pipelineId,
    thread_id: 'thread-runfailed-t',
    status: 'running',
    started_at: '2026-09-28T00:00:00Z',
  }
  updatePipelineRunsCache((prev) => ({ ...prev, [pipelineId]: run }))
}

function resetStores(): void {
  useNotificationStore.setState({
    notifications: [],
    groupState: { collapsed: { critical: false, high: false, normal: true, low: true } },
    isPanelOpen: false,
    activeBlockingNotification: null,
  })
  usePipelineMessageStore.setState({
    messagesByPipeline: {},
    streamingState: {},
    pipelines: {},
    pipelineSessionMap: {},
    activePipelineId: null,
  } as never)
  vi.useRealTimers()
}

describe('handleRunFailed — 失败弹卡', () => {
  beforeEach(resetStores)

  it('署名失败（stop_reason=tool_fail_loop）→ 弹 high/error 卡，消息含署名与路由坐标', () => {
    seedRun(PID_SIGNED, 'run-sig-1')
    handleRunFailed(runFailedEvent(PID_SIGNED, 'run-sig-1', 'tool_fail_loop') as never)

    const ns = useNotificationStore.getState().notifications
    expect(ns).toHaveLength(1)
    const [n] = ns
    expect(n.id).toBe(`run-failed-${PID_SIGNED}-run-sig-1`)
    expect(n.priority).toBe('high')
    expect(n.category).toBe('error')
    expect(n.isBlocking).toBe(false)
    expect(n.message).toContain('tool_fail_loop')
    expect(n.sessionId).toBe('thread-runfailed-t')
  })

  it('引擎 Err 形态（stop_reason=null）→ 同样弹卡，消息不出现 null/undefined 字样', () => {
    handleRunFailed(runFailedEvent(PID_ENGINE, 'run-eng-1', null) as never)
    const ns = useNotificationStore.getState().notifications
    expect(ns).toHaveLength(1)
    expect(ns[0].message).not.toContain('null')
    expect(ns[0].message).not.toContain('undefined')
    expect(ns[0].id).toBe(`run-failed-${PID_ENGINE}-run-eng-1`)
  })

  it('副作用：runs 缓存标 failed + streamingState 清理（未注册管道零副作用不抛错）', () => {
    seedRun(PID_SIDEEFFECT, 'run-sig-2')
    usePipelineMessageStore.setState({
      streamingState: { [PID_SIDEEFFECT]: { isStreaming: true } as never },
    } as never)
    handleRunFailed(runFailedEvent(PID_SIDEEFFECT, 'run-sig-2', 'duplicate_loop') as never)

    expect(readPipelineRuns()[PID_SIDEEFFECT]?.status).toBe('failed')
    expect(usePipelineMessageStore.getState().streamingState[PID_SIDEEFFECT]).toBeUndefined()

    // 未注册管道（无 runs 缓存条目）：照常弹卡，注册表建最小兜底行标 failed
    // （applyStreamStatus 对未知管道的既有行为，与 stream_error 同款）
    expect(() => handleRunFailed(runFailedEvent(PID_MISSING, 'run-m-1', 'timeout') as never)).not.toThrow()
    expect(useNotificationStore.getState().notifications).toHaveLength(2)
    expect(readPipelineRuns()[PID_MISSING]?.status).toBe('failed')
  })

  it('无 pipeline_id → 跳过（零通知零副作用）', () => {
    handleRunFailed({ data: { thread_id: 't', status: 'failed', run_id: 'r' } } as never)
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })
})

describe('handleRunFailed — 幂等与交叉去重', () => {
  beforeEach(resetStores)

  it('同事件重放（同 run_id）→ 两层去重合计只留一张卡：窗内短窗去重 + 跨窗 id 合并', () => {
    vi.useFakeTimers()
    const ev = runFailedEvent(PID_REPLAY, 'run-replay-1', 'timeout')
    // 窗内三次投递（重连快速重放）：首次弹卡，后续被短窗去重
    handleRunFailed(ev as never)
    handleRunFailed(ev as never)
    handleRunFailed(ev as never)
    // 跨窗重放（断线超 15s 后重连）：短窗过期，落到通知中心同 id 合并
    vi.advanceTimersByTime(16_000)
    handleRunFailed(ev as never)
    const ns = useNotificationStore.getState().notifications
    expect(ns.filter((n) => n.id === `run-failed-${PID_REPLAY}-run-replay-1`)).toHaveLength(1)
  })

  it('run_failed 先到 + stream_error 后到（ENGINE_RUN_FAILED 生产时序）→ 只弹一张失败卡，消息仍标 error', () => {
    usePipelineMessageStore.getState().addMessage(PID_DEDUP, {
      id: 'msg-dedup-1',
      role: 'assistant',
      content: '部分输出',
      parts: [{ type: 'text', content: '部分输出', state: 'streaming' }],
      status: 'streaming',
    } as never)

    handleRunFailed(runFailedEvent(PID_DEDUP, 'run-dedup-1', null) as never)
    handleStreamError({
      data: {
        pipeline_id: PID_DEDUP,
        message_id: 'msg-dedup-1',
        thread_id: 'thread-runfailed-t',
        error: { code: 'ENGINE_RUN_FAILED', message: '引擎执行失败' },
      },
    } as never)

    const ns = useNotificationStore.getState().notifications
    expect(ns.filter((n) => n.category === 'error')).toHaveLength(1)
    expect(ns[0].id).toBe(`run-failed-${PID_DEDUP}-run-dedup-1`)
    // 去重只作用于通知卡：消息级失败标记照常生效
    const msg = usePipelineMessageStore.getState().getMessages(PID_DEDUP)
      .find((m: any) => m.id === 'msg-dedup-1')
    expect((msg as any)?.status).toBe('error')
  })

  it('反序（stream_error 先到 + run_failed 后到）→ 仍只有一张失败卡', () => {
    usePipelineMessageStore.getState().addMessage(PID_DEDUP_REV, {
      id: 'msg-dedup-2',
      role: 'assistant',
      content: '',
      parts: [],
      status: 'streaming',
    } as never)
    handleStreamError({
      data: {
        pipeline_id: PID_DEDUP_REV,
        message_id: 'msg-dedup-2',
        thread_id: 'thread-runfailed-t',
        error: '流式异常',
      },
    } as never)
    handleRunFailed(runFailedEvent(PID_DEDUP_REV, 'run-dedup-2', null) as never)

    const ns = useNotificationStore.getState().notifications
    expect(ns.filter((n) => n.category === 'error')).toHaveLength(1)
  })
})
