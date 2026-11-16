/** @feature FP-T12 前端适配 | @ci: frontend-test */
// @ci: frontend-test
/**
 * new_message 系统通知接线测试（壳能力接线：判定器驱动 v3）。
 *
 * 决策契约（lib/systemNotifier，v3）：
 * - 消息 metadata.notify_worthy===true（后端判定器标记，键名契约）：弹（无视
 *   可见性/焦点，前台也弹）；
 * - 无标记：一律静默不弹（前台后台都不弹——可见性兜底已撤销，防 away 时
 *   管道消息轰炸；触发器/注入轮是否弹出由判定器打标接管，前端不看 proactive）；
 * - 同消息 id 重复投递只弹一次；桥接失败不阻塞消息落定；
 * - 事件 _threadId 随通知透传（壳层点击跳转对应对话的载荷）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { makeEventFactory } from './helpers/streamingEventFactory'
import { resetStreamingStore } from './helpers/streamingStoreKit'
import type * as sessionMod from '@/services/api/session'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<sessionMod>()
  return {
    ...actual,
    getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
  }
})

const PIPELINE_ID = 'pipe-sysnotify-001'
const THREAD_ID = 'thread-sysnotify-001'

const makeEvent = makeEventFactory(PIPELINE_ID, 'msg-sysnotify')

/** 后端落库 assistant 消息完整形态（与 ws_session new_message.message 同构） */
function makeServerMessage(overrides: Record<string, any> = {}) {
  return {
    id: 'msg-sysnotify',
    role: 'assistant',
    content: '最终回答文本',
    sequence: 5,
    timestamp: '2026-10-14T00:00:00Z',
    status: 'completed',
    thread_id: THREAD_ID,
    ...overrides,
  }
}

function setBridge(show: ReturnType<typeof vi.fn> | undefined): void {
  if (show === undefined) {
    delete (window as { electronAPI?: unknown }).electronAPI
    return
  }
  ;(window as { electronAPI?: unknown }).electronAPI = { notification: { show } }
}

function setDocumentState(visibilityState: 'visible' | 'hidden', hasFocus: boolean): void {
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue(visibilityState)
  vi.spyOn(document, 'hasFocus').mockReturnValue(hasFocus)
}

describe('new_message 系统通知接线（标记必弹 / 无标记静默）', () => {
  let handleNewMessage: any
  let handleStreamStart: any

  beforeEach(async () => {
    await resetStreamingStore(PIPELINE_ID, THREAD_ID)
    localStorage.removeItem('notification_sound_muted')
    const handlerMod = await import('@/services/websocket/streaming/handlers')
    handleNewMessage = handlerMod.handleNewMessage
    handleStreamStart = handlerMod.handleStreamStart
    // 占位消息由 stream_start 创建（applyServerPayload 要求已存在）
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))
  })

  afterEach(() => {
    vi.restoreAllMocks()
    setBridge(undefined)
  })

  it('notify_worthy 标记（普通消息带 cmid）+ 前台正在看：必弹（无视可见性/焦点）', async () => {
    setDocumentState('visible', true)
    const show = vi.fn(async () => true)
    setBridge(show)

    handleNewMessage(makeEvent('new_message', {
      sequence: 5,
      content: '判定器认定值得打断的回复',
      message: makeServerMessage({
        content: '判定器认定值得打断的回复',
        metadata: { notify_worthy: true },
      }),
      client_message_id: 'cmid-nw-1',
      user_message: { id: 'rec_nw_1', content: '问题', sequence: 4 },
    }))

    await vi.waitFor(() => expect(show).toHaveBeenCalledTimes(1))
    expect(show).toHaveBeenCalledWith({ title: '灵汐', body: '判定器认定值得打断的回复' })
  })

  it('notify_worthy 标记（触发器注入轮无 cmid）+ 前台可见：弹（主动消息弹出由判定器打标接管）', async () => {
    setDocumentState('visible', true)
    const show = vi.fn(async () => true)
    setBridge(show)

    handleNewMessage(makeEvent('new_message', {
      sequence: 5,
      content: '最终回答文本',
      message: makeServerMessage({ metadata: { notify_worthy: true } }),
      user_message: { id: 'rec_trig_1', content: '【日程提醒】下午三点开会', sequence: 4 },
    }))

    await vi.waitFor(() => expect(show).toHaveBeenCalledTimes(1))
    expect(show).toHaveBeenCalledWith({ title: '灵汐', body: '最终回答文本' })
  })

  it('notify_worthy 标记 + 文档隐藏：弹', async () => {
    setDocumentState('hidden', true)
    const show = vi.fn(async () => true)
    setBridge(show)

    handleNewMessage(makeEvent('new_message', {
      sequence: 5,
      content: '后台必弹回复',
      message: makeServerMessage({
        content: '后台必弹回复',
        metadata: { notify_worthy: true },
      }),
      client_message_id: 'cmid-nw-2',
      user_message: { id: 'rec_nw_2', content: '问题', sequence: 4 },
    }))

    await vi.waitFor(() => expect(show).toHaveBeenCalledTimes(1))
    expect(show).toHaveBeenCalledWith({ title: '灵汐', body: '后台必弹回复' })
  })

  it('无标记 + 文档隐藏：不弹（v3 撤销可见性兜底——后台碎语静默）', async () => {
    setDocumentState('hidden', true)
    const show = vi.fn(async () => true)
    setBridge(show)

    handleNewMessage(makeEvent('new_message', {
      sequence: 5,
      content: '后台完成回复',
      message: makeServerMessage({ content: '后台完成回复' }),
      client_message_id: 'cmid-2',
      user_message: { id: 'rec_2', content: '问题', sequence: 4 },
    }))

    await new Promise((r) => setTimeout(r, 0))
    expect(show).not.toHaveBeenCalled()
  })

  it('无标记的触发器注入轮（无 cmid）+ 前台可见：不弹（proactive 不再前端兜底）', async () => {
    setDocumentState('visible', true)
    const show = vi.fn(async () => true)
    setBridge(show)

    handleNewMessage(makeEvent('new_message', {
      sequence: 5,
      content: '最终回答文本',
      message: makeServerMessage(),
      user_message: { id: 'rec_trig_2', content: '【日程提醒】下午三点开会', sequence: 4 },
    }))

    await new Promise((r) => setTimeout(r, 0))
    expect(show).not.toHaveBeenCalled()
  })

  it('metadata 存在但 notify_worthy 非 true：视作无标记走静默（前台不弹）', async () => {
    setDocumentState('visible', true)
    const show = vi.fn(async () => true)
    setBridge(show)

    handleNewMessage(makeEvent('new_message', {
      sequence: 5,
      content: '普通回复',
      message: makeServerMessage({
        metadata: { notify_worthy: false, other_key: 'x' },
      }),
      client_message_id: 'cmid-nw-3',
      user_message: { id: 'rec_nw_3', content: '问题', sequence: 4 },
    }))

    await new Promise((r) => setTimeout(r, 0))
    expect(show).not.toHaveBeenCalled()
  })

  it('notify_worthy 标记 + 前台：同消息 id 重复投递只弹一次（dedupe 先于必弹规则）', async () => {
    setDocumentState('visible', true)
    const show = vi.fn(async () => true)
    setBridge(show)

    const event = makeEvent('new_message', {
      sequence: 5,
      content: '必弹回复',
      message: makeServerMessage({
        content: '必弹回复',
        metadata: { notify_worthy: true },
      }),
      client_message_id: 'cmid-nw-4',
      user_message: { id: 'rec_nw_4', content: '问题', sequence: 4 },
    })
    handleNewMessage(event)
    await vi.waitFor(() => expect(show).toHaveBeenCalledTimes(1))
    // 同 id 重放：即使标记仍在、窗口切后台，也不重复弹
    setDocumentState('hidden', true)
    handleNewMessage(event)
    await new Promise((r) => setTimeout(r, 0))
    expect(show).toHaveBeenCalledTimes(1)
  })

  it('事件携带 _threadId → 通知透传该会话 threadId（点击通知跳转对应对话）', async () => {
    setDocumentState('hidden', true)
    const show = vi.fn(async () => true)
    setBridge(show)

    handleNewMessage(makeEvent('new_message', {
      sequence: 5,
      _threadId: 'thread-nav-9',
      content: '带会话坐标的回复',
      message: makeServerMessage({
        content: '带会话坐标的回复',
        metadata: { notify_worthy: true },
      }),
      client_message_id: 'cmid-nw-6',
      user_message: { id: 'rec_nw_6', content: '问题', sequence: 4 },
    }))

    await vi.waitFor(() => expect(show).toHaveBeenCalledTimes(1))
    expect(show).toHaveBeenCalledWith({
      title: '灵汐',
      body: '带会话坐标的回复',
      threadId: 'thread-nav-9',
    })
  })

  it('桥接异常：通知失败静默降级，消息照常落定不抛错', async () => {
    setDocumentState('hidden', true)
    setBridge(vi.fn(async () => { throw new Error('ipc down') }))

    expect(() =>
      handleNewMessage(makeEvent('new_message', {
        sequence: 5,
        content: '最终回答文本',
        message: makeServerMessage({ metadata: { notify_worthy: true } }),
        user_message: { id: 'rec_trig_4', content: '触发', sequence: 4 },
      })),
    ).not.toThrow()

    const store = (window as any).__pipelineStore
    const msg = store.getState().getMessages(PIPELINE_ID).find((m: any) => m.id === 'msg-sysnotify')
    expect(msg?.content).toBe('最终回答文本')
    // 异步链路的拒绝也不得上抛为 unhandled rejection（notifyPipelineMessage 永不 reject）
    await new Promise((r) => setTimeout(r, 0))
  })

  it('非 Electron 环境（无桥接）：决策照常执行，静默折 false 不报错', async () => {
    setDocumentState('hidden', true)
    setBridge(undefined)

    expect(() =>
      handleNewMessage(makeEvent('new_message', {
        sequence: 5,
        content: '最终回答文本',
        message: makeServerMessage({ metadata: { notify_worthy: true } }),
        user_message: { id: 'rec_trig_5', content: '触发', sequence: 4 },
      })),
    ).not.toThrow()
  })
})
