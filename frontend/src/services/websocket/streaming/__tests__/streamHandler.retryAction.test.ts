// @feature FP-T12 前端适配（P-26 失败通知卡一键重试） | @ci frontend-test
/**
 * 失败通知卡重试动作（P-26，三清单清单二 15 项）行为测试：
 * - stream_error 信封 retryable=true → 失败卡附「重新发送」动作 + 会话坐标
 *   （动作=custom+payload.retrySessionId，NotificationCenter 分发定位到会话；
 *   重发是用户语义动作，只定位不代发——提案被否方案）
 * - 信封 retryable=false / 旧形态字符串 error → 无动作（非 retryable 无动作）
 * - plugin_error 信封消费同款：retryable=false（内核现值恒 false）→ 无动作；
 *   信封 retryable=true → 动作出现（消费信封元数据而非硬编码内核现值）
 * - run_failed 无错误信封（error=纯字符串，内核 emit_run_failed_frontend_event
 *   实证）→ 无 retryable 场景可消费 → 无动作
 *
 * 全部走真实 zustand store + 全局 queryClient 单例，不 mock 内部依赖。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import {
  handlePluginError,
  handleRunFailed,
  handleStreamError,
} from '../handlers/streamHandler'

// 模块级失败卡去重账本跨用例存活：每用例独立管道 id 隔离（生产语义=不同管道）。
const PID_RETRY = 'pipe-retry-pos-0000000000000000000001'
const PID_NORETRY = 'pipe-retry-neg-0000000000000000000002'
const PID_OLDSTR = 'pipe-retry-old-0000000000000000000003'
const PID_PLUGIN = 'pipe-retry-plugin-00000000000000000004'
const PID_PLUGIN_R = 'pipe-retry-pluginr-0000000000000000005'
const PID_RUNFAIL = 'pipe-retry-runf-000000000000000000006'

function streamErrorEvent(pipelineId: string, error: unknown, threadId = 'thread-retry-t') {
  return {
    data: {
      pipeline_id: pipelineId,
      _threadId: threadId,
      error,
    },
  }
}

function pluginErrorEvent(pipelineId: string, error: unknown, threadId = 'thread-plugin-t') {
  return {
    data: {
      pipeline_id: pipelineId,
      _threadId: threadId,
      plugin_id: 'demo_plugin',
      error,
    },
  }
}

function resetStores(): void {
  useNotificationStore.setState({
    notifications: [],
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

/** 断言通知不带任何重试坐标/动作（非 retryable 场景负例） */
function expectNoRetryAction(): void {
  const ns = useNotificationStore.getState().notifications
  expect(ns).toHaveLength(1)
  expect(ns[0].actions).toBeUndefined()
  expect(ns[0].sessionId).toBeUndefined()
}

describe('handleStreamError — 失败卡重试动作（P-26）', () => {
  beforeEach(resetStores)

  it('信封 retryable=true → 卡片附「重新发送」动作 + 会话坐标（正例）', () => {
    handleStreamError(
      streamErrorEvent(PID_RETRY, {
        code: 'SERVICE_UNAVAILABLE',
        message: '服务暂不可用（retry-case-1）',
        source: 'kernel',
        retryable: true,
      }),
    )

    const ns = useNotificationStore.getState().notifications
    expect(ns).toHaveLength(1)
    const [n] = ns
    expect(n.priority).toBe('high')
    expect(n.category).toBe('error')
    expect(n.sessionId).toBe('thread-retry-t')
    expect(n.actions).toHaveLength(1)
    const action = n.actions![0]
    expect(action.label).toBe('重新发送')
    expect(action.action).toBe('custom')
    expect(action.payload).toMatchObject({ retrySessionId: 'thread-retry-t' })
  })

  it('信封 retryable=false → 无动作无会话坐标（非 retryable 负例）', () => {
    handleStreamError(
      streamErrorEvent(PID_NORETRY, {
        code: 'BAD_REQUEST',
        message: '请求参数错误（retry-case-2）',
        source: 'kernel',
        retryable: false,
      }),
    )
    expectNoRetryAction()
  })

  it('旧形态字符串 error → 无动作（无信封即无 retryable 元数据）', () => {
    handleStreamError(streamErrorEvent(PID_OLDSTR, '流式响应异常（retry-case-3）'))
    expectNoRetryAction()
  })
})

describe('handlePluginError — 插件错误卡信封消费（P-26 同款）', () => {
  beforeEach(resetStores)

  it('信封 retryable=false（内核现值恒 false）→ 无动作', () => {
    handlePluginError(
      pluginErrorEvent(PID_PLUGIN, {
        code: 'PLUGIN_EXEC_FAILED',
        message: '插件执行失败（retry-case-4）',
        source: 'plugin',
        retryable: false,
      }),
    )
    expectNoRetryAction()
  })

  it('信封 retryable=true → 动作出现（消费信封元数据）', () => {
    handlePluginError(
      pluginErrorEvent(PID_PLUGIN_R, {
        code: 'TOO_MANY_REQUESTS',
        message: '插件限流（retry-case-5）',
        source: 'plugin',
        retryable: true,
      }),
    )

    const ns = useNotificationStore.getState().notifications
    expect(ns).toHaveLength(1)
    expect(ns[0].sessionId).toBe('thread-plugin-t')
    expect(ns[0].actions).toHaveLength(1)
    expect(ns[0].actions![0].label).toBe('重新发送')
    expect(ns[0].actions![0].action).toBe('custom')
    expect(ns[0].actions![0].payload).toMatchObject({ retrySessionId: 'thread-plugin-t' })
  })
})

describe('handleRunFailed — 运行失败卡（P-26 边界）', () => {
  beforeEach(resetStores)

  it('error=纯字符串（无信封无 retryable）→ 无动作（内核 run_failed 载荷实证）', () => {
    handleRunFailed({
      data: {
        pipeline_id: PID_RUNFAIL,
        _threadId: 'thread-runfailed-t',
        status: 'failed',
        stop_reason: 'tool_fail_loop',
        error: '插件 boom（retry-case-6）',
        run_id: 'run-retry-1',
      },
    } as never)

    const ns = useNotificationStore.getState().notifications
    expect(ns).toHaveLength(1)
    // 既有行为：sessionId 坐标在位（点击条目定位会话），但无重试动作
    expect(ns[0].sessionId).toBe('thread-runfailed-t')
    expect(ns[0].actions).toBeUndefined()
  })
})
