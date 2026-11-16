/** @feature FP-0.2.待发送队列 强制发送/退回输入框 | @ci: frontend-test */
/** @ci: frontend-test */
/**
 * pendingForceSend 服务测试（2026-10-27 权威化单帧原语）
 *
 * 覆盖契约：
 * - 正常路径：本地队列视图命中目标 → 单帧 sendForcePendingInput 出站
 *   （编排本体在内核 dispatch_force_pending：目标置队首 + 停当前回复不退回
 *   队列 + drain 首个消费目标）；前端零 DELETE 零停止零重发——旧「DELETE→
 *   停止→重发」编排会把其余排队条目一并退回输入框（2026-10-27 用户实测），
 *   已退役。
 * - 目标条目不在本地队列视图：零 WS 发送 + 权威 GET 对账 + 通知
 *   （防重复发送）。
 *
 * GlobalWebSocket 是外部依赖（网络面），mock 替换；store 用真实实现断言。
 */
import { waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { sendForcePendingInput: vi.fn() },
}))

import { forceSendPendingInput } from '@/services/pendingForceSend'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePendingInputStore } from '@/stores/pendingInputStore'

const queued = (id: string, content: string) => ({
  id,
  pipeline_id: 'pipe-1',
  content,
  source: 'user' as const,
  created_at: '2026-10-19T00:00:00Z',
})

describe('forceSendPendingInput', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    usePendingInputStore.setState({ byPipeline: {} })
    useNotificationStore.getState().clearAll()
  })

  it('正常路径：本地命中目标 → 单帧 sendForcePendingInput 出站', async () => {
    usePendingInputStore.setState({
      byPipeline: { 'pipe-1': [queued('a', '目标消息'), queued('b', '其他消息')] },
    })

    await forceSendPendingInput({ sessionId: 'sess-1', pipelineId: 'pipe-1', inputId: 'a' })

    expect(globalWS.sendForcePendingInput).toHaveBeenCalledTimes(1)
    expect(globalWS.sendForcePendingInput).toHaveBeenCalledWith('sess-1', {
      pipelineId: 'pipe-1',
      inputId: 'a',
    })
  })

  it('目标条目不在本地队列视图：零 WS 发送 + 权威 GET 对账 + 通知', async () => {
    usePendingInputStore.setState({
      byPipeline: { 'pipe-1': [queued('b', '其他消息')] },
    })
    const loadSpy = vi.fn().mockResolvedValue(undefined)
    usePendingInputStore.setState({ load: loadSpy } as never)

    await forceSendPendingInput({ sessionId: 'sess-1', pipelineId: 'pipe-1', inputId: 'ghost' })

    expect(globalWS.sendForcePendingInput).not.toHaveBeenCalled()
    expect(loadSpy).toHaveBeenCalledWith('pipe-1')
    await waitFor(() => {
      const notif = useNotificationStore
        .getState()
        .notifications.find((n) => n.title === '未发送该消息')
      expect(notif).toBeDefined()
    })
  })
})
