/** @feature: FP-T12 前端适配（发送终局消费 hook） | @ci: frontend-test */
/**
 * useSendOutcomeConsumer 测试：store 三类终局（landed/timeout/failed）× 归属
 * 判定面的消费行为。
 * - landed：本管道在途清零 + 文本未变 → onLanded；在途未清 / 文本已改写 → 不动作；
 * - timeout：onError 固定判死文案；failed：带原因与无原因双文案；
 * - 归属：草稿键不匹配的 outcome 跳过；无草稿键按快照文本匹配；
 * - id 增量：同一 outcome 重复入列不重复消费（重挂载重放历史终局收敛视图）。
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useSendOutcomeConsumer } from '@/hooks/useSendOutcomeConsumer'
import { useChatInputStore, type SendInFlightEntry, type SendOutcome } from '@/stores/chatInputStore'

const outcome = (id: number, over: Partial<SendOutcome> = {}): SendOutcome => ({
  id,
  clientMessageId: `cmid-${id}`,
  pipelineId: 'pipe-1',
  kind: 'landed',
  content: '你好',
  ...over,
})

const inFlight = (content: string): SendInFlightEntry => ({
  clientMessageId: 'cmid-x',
  content,
  startedAt: Date.now(),
})

function setupRefs(text = '你好') {
  return { current: text }
}

beforeEach(() => {
  useChatInputStore.setState({ sendOutcomes: [], sendInFlightByPipeline: {} })
})

afterEach(() => {
  useChatInputStore.setState({ sendOutcomes: [], sendInFlightByPipeline: {} })
})

describe('useSendOutcomeConsumer（发送终局消费）', () => {
  it('landed：在途清零且文本未变 → 清空输入面', () => {
    const textRef = setupRefs('你好')
    const onLanded = vi.fn()
    const onError = vi.fn()
    renderHook(() => useSendOutcomeConsumer({ draftKey: 'd1', textRef, onLanded, onError }))

    act(() => {
      useChatInputStore.setState({ sendOutcomes: [outcome(1, { draftKey: 'd1' })] })
    })
    expect(onLanded).toHaveBeenCalledTimes(1)
    expect(onError).not.toHaveBeenCalled()
  })

  it('landed：在途未清（同管道仍有条目）→ 不清空', () => {
    const textRef = setupRefs('你好')
    const onLanded = vi.fn()
    renderHook(() =>
      useSendOutcomeConsumer({ textRef, onLanded, onError: vi.fn() }),
    )

    act(() => {
      useChatInputStore.setState({
        sendInFlightByPipeline: { 'pipe-1': [inFlight('你好')] },
        sendOutcomes: [outcome(2)],
      })
    })
    expect(onLanded).not.toHaveBeenCalled()
  })

  it('landed：文本已被改写（快照不匹配）→ 不清空', () => {
    const textRef = setupRefs('改过的新文本')
    const onLanded = vi.fn()
    renderHook(() => useSendOutcomeConsumer({ textRef, onLanded, onError: vi.fn() }))

    act(() => {
      useChatInputStore.setState({ sendOutcomes: [outcome(3)] })
    })
    expect(onLanded).not.toHaveBeenCalled()
  })

  it('timeout：判死文案落操作点（不触发清空）', () => {
    const textRef = setupRefs('你好')
    const onLanded = vi.fn()
    const onError = vi.fn()
    renderHook(() => useSendOutcomeConsumer({ draftKey: 'd1', textRef, onLanded, onError }))

    act(() => {
      useChatInputStore.setState({
        sendOutcomes: [outcome(4, { kind: 'timeout', draftKey: 'd1' })],
      })
    })
    expect(onError).toHaveBeenCalledWith('发送超时，已停止执行——请重新发送')
    expect(onLanded).not.toHaveBeenCalled()
  })

  it('failed：带原因与无原因双文案', () => {
    const textRef = setupRefs('你好')
    const onError = vi.fn()
    renderHook(() => useSendOutcomeConsumer({ textRef, onLanded: vi.fn(), onError }))

    act(() => {
      useChatInputStore.setState({
        sendOutcomes: [outcome(5, { kind: 'failed', reason: 'ack 拒收' })],
      })
    })
    expect(onError).toHaveBeenCalledWith('消息发送失败：ack 拒收')

    act(() => {
      useChatInputStore.setState({
        sendOutcomes: [outcome(5, { kind: 'failed', reason: 'ack 拒收' }), outcome(6, { kind: 'failed' })],
      })
    })
    expect(onError).toHaveBeenLastCalledWith('消息发送失败，请重新发送')
    expect(onError).toHaveBeenCalledTimes(2)
  })

  it('归属：草稿键不匹配跳过，无草稿键按快照文本匹配', () => {
    const textRef = setupRefs('你好')
    const onLanded = vi.fn()
    const onError = vi.fn()
    renderHook(() => useSendOutcomeConsumer({ draftKey: 'mine', textRef, onLanded, onError }))

    act(() => {
      useChatInputStore.setState({
        // 别的输入器（草稿键不同）的终局：与本实例无关
        sendOutcomes: [outcome(7, { draftKey: 'other', kind: 'timeout' })],
      })
    })
    expect(onError).not.toHaveBeenCalled()

    act(() => {
      useChatInputStore.setState({
        // 无草稿键：按受理快照文本归属，匹配本实例正文 → 消费
        sendOutcomes: [
          outcome(7, { draftKey: 'other', kind: 'timeout' }),
          outcome(8, { kind: 'failed', reason: '网络断开' }),
        ],
      })
    })
    expect(onError).toHaveBeenCalledWith('消息发送失败：网络断开')
  })

  it('挂载即消费历史终局（重放收敛视图）且同 id 不重复消费', () => {
    const textRef = setupRefs('你好')
    const onLanded = vi.fn()
    useChatInputStore.setState({ sendOutcomes: [outcome(9, { draftKey: 'd1' })] })

    renderHook(() =>
      useSendOutcomeConsumer({ draftKey: 'd1', textRef, onLanded, onError: vi.fn() }),
    )
    expect(onLanded).toHaveBeenCalledTimes(1)

    // 同一 outcome 仍在序列里（新终局追加触发重扫）：id 已消费 → 不重复清空
    act(() => {
      useChatInputStore.setState({
        sendOutcomes: [outcome(9, { draftKey: 'd1' }), outcome(10, { draftKey: 'd1' })],
      })
    })
    expect(onLanded).toHaveBeenCalledTimes(2)
  })

  it('卸载后退订：后续终局不再回调', () => {
    const textRef = setupRefs('你好')
    const onLanded = vi.fn()
    const { unmount } = renderHook(() =>
      useSendOutcomeConsumer({ textRef, onLanded, onError: vi.fn() }),
    )
    unmount()
    act(() => {
      useChatInputStore.setState({ sendOutcomes: [outcome(11)] })
    })
    expect(onLanded).not.toHaveBeenCalled()
  })
})
