// @feature: FP-T12 前端适配(‹i/n›代际切换器) | @ci: frontend-test
// @feature: message-segments P1 | @ci: frontend-test
/**
 * ‹i/n› 多代切换器（消息段模型 P1）。
 *
 * 覆盖（[来源: docs/working/消息历史双能力方案_多代切换与压缩原文_20260923.md §2.5/§5/§7.3]）：
 * - 锚点计算（utils/segmentAnchor）：max base_seq ≤ seq、轮末条 assistant、count>1 才显示；
 * - 切换器渲染：锚点多段才出现，位置标签 当前/n 与 k/n；
 * - 切换乐观链路：GET 段详情 → applySegmentView 换装 → segment_activate 出站；
 * - 运行中拒绝：toast 提示、不换装、不出站（不改启用状态）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { anchorBaseSeqOf, collectSegmentSwitcherAnchors } from '@/utils/segmentAnchor'
import { MessageActions } from '../MessageActions'
import type { SegmentDetail, SegmentMeta } from '@/services/api/messageSegments'
import type { Message } from '@/types/models'

const toastErrorMock = vi.fn()
vi.mock('sonner', () => ({
  toast: { error: (...args: unknown[]) => toastErrorMock(...args), success: vi.fn() },
}))

const sendSegmentActivateMock = vi.fn()
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { sendSegmentActivate: (...args: unknown[]) => sendSegmentActivateMock(...args) },
}))

const getMessageSegmentDetailMock = vi.fn()
const getMessageSegmentsMock = vi.fn().mockResolvedValue([])
vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegments: (...args: unknown[]) => getMessageSegmentsMock(...args),
  getMessageSegmentDetail: (...args: unknown[]) => getMessageSegmentDetailMock(...args),
}))

function makeSegment(id: string, base_seq: number, created_at: string): SegmentMeta {
  return { id, base_seq, base_len: 2, visible_to: '', preview: null, created_at }
}

function makeMessage(role: Message['role'], sequence: number | null, overrides: Partial<Message> = {}): Message {
  return {
    id: `msg-${sequence ?? 'x'}`,
    sessionId: 'sess-1',
    sequence: sequence ?? undefined,
    role,
    content: '内容',
    timestamp: '2026-09-23T00:00:00Z',
    parentId: null,
    status: 'completed',
    ...overrides,
  } as Message
}

const SEGMENTS_TWO_GENS = [
  makeSegment('seg-gen1', 50, '2026-09-23T00:00:00Z'),
  makeSegment('seg-gen2', 50, '2026-09-23T02:00:00Z'),
]

function seedStore(opts?: { segments?: SegmentMeta[]; streaming?: boolean }) {
  const ps = usePipelineMessageStore.getState()
  usePipelineMessageStore.setState({
    activePipelineId: 'pipe-1',
    pipelineSessionMap: { 'pipe-1': 'sess-1' },
    spanIndex: { 'pipe-1': opts?.segments ?? SEGMENTS_TWO_GENS },
    segmentViewByPipeline: {},
    streamingState: opts?.streaming
      ? { 'pipe-1': { isStreaming: true, messageId: 'm-live', startedAt: Date.now() } as never }
      : {},
    messagesByPipeline: {
      'pipe-1': [makeMessage('user', 49, { id: 'msg-keep' }), makeMessage('user', 50), makeMessage('assistant', 51)],
    },
    topCursorsByPipeline: ps.topCursorsByPipeline,
  })
}

describe('segmentAnchor 锚点计算', () => {
  const segments = [
    makeSegment('seg-a', 50, '2026-09-23T00:00:00Z'),
    makeSegment('seg-b', 50, '2026-09-23T01:00:00Z'),
    makeSegment('seg-c', 66, '2026-09-23T02:00:00Z'),
  ]

  it('anchorBaseSeqOf = 覆盖该消息的最新替换点（max base_seq ≤ seq）；无替换点为 null', () => {
    expect(anchorBaseSeqOf(segments, 70)).toBe(66)
    expect(anchorBaseSeqOf(segments, 66)).toBe(66)
    expect(anchorBaseSeqOf(segments, 65)).toBe(50)
    expect(anchorBaseSeqOf(segments, 10)).toBeNull()
  })

  it('collectSegmentSwitcherAnchors：轮末条 assistant 持锚点，count>1 才收录', () => {
    const messages = [
      makeMessage('user', 49),
      makeMessage('assistant', 51),
      makeMessage('assistant', 55), // 锚点 50 的轮末条 assistant
      makeMessage('user', 66),
      makeMessage('assistant', 70), // 锚点 66 仅 1 段 → 不收录
    ]
    const anchors = collectSegmentSwitcherAnchors(messages, segments)
    expect(anchors.size).toBe(1)
    expect(anchors.get('msg-55')).toBe(50)
    expect(anchors.has('msg-70')).toBe(false)
  })

  it('collectSegmentSwitcherAnchors：段数 <2 整体不产生锚点；无 sequence 消息跳过', () => {
    expect(collectSegmentSwitcherAnchors([makeMessage('assistant', 55)], [segments[0]]).size).toBe(0)
    const noSeq = [makeMessage('assistant', 51), makeMessage('assistant', 55), makeMessage('assistant', null)]
    // 无 sequence 的 assistant 不参与锚定（乐观/流式占位）
    const anchors = collectSegmentSwitcherAnchors(noSeq, segments)
    expect(anchors.get('msg-55')).toBe(50)
    expect(anchors.has('msg-x')).toBe(false)
  })
})

describe('‹i/n› 切换器渲染（count>1 才显示）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    seedStore()
  })

  it('锚点两段 → 渲染 ‹ 当前/2 ›，启用序列位 next 禁用、prev 可点', () => {
    render(
      <MessageActions
        message={makeMessage('assistant', 51)}
        sessionId="sess-1"
        segmentSwitcherBaseSeq={50}
      />,
    )
    expect(screen.getByTestId('segment-switcher')).toBeInTheDocument()
    expect(screen.getByTestId('segment-position')).toHaveTextContent('当前/2')
    expect(screen.getByTestId('segment-prev')).toBeEnabled()
    expect(screen.getByTestId('segment-next')).toBeDisabled()
  })

  it('锚点仅一段 → 不渲染切换器（count>1 才显示）', () => {
    seedStore({ segments: [makeSegment('seg-only', 50, '2026-09-23T00:00:00Z')] })
    render(
      <MessageActions
        message={makeMessage('assistant', 51)}
        sessionId="sess-1"
        segmentSwitcherBaseSeq={50}
      />,
    )
    expect(screen.queryByTestId('segment-switcher')).not.toBeInTheDocument()
  })

  it('未标定锚点（null）→ 不渲染切换器', () => {
    render(<MessageActions message={makeMessage('assistant', 51)} sessionId="sess-1" />)
    expect(screen.queryByTestId('segment-switcher')).not.toBeInTheDocument()
  })

  it('乐观段视图激活中 → 位置标签显示代号 k/n', () => {
    usePipelineMessageStore.setState({
      segmentViewByPipeline: { 'pipe-1': { baseSeq: 50, segmentId: 'seg-gen2' } },
    })
    render(
      <MessageActions
        message={makeMessage('assistant', 51)}
        sessionId="sess-1"
        segmentSwitcherBaseSeq={50}
      />,
    )
    expect(screen.getByTestId('segment-position')).toHaveTextContent('2/2')
    // 第 n 代（最新冻结）next 禁用，prev 仍可继续往更早走
    expect(screen.getByTestId('segment-next')).toBeDisabled()
    expect(screen.getByTestId('segment-prev')).toBeEnabled()
  })
})

describe('切换乐观 + 出站（ack 对账由 useRealtimeEvents 契约测试覆盖）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    seedStore()
  })

  // seg-gen2（最新冻结代，created_at 较晚）＝启用序列的上一代；其成员即"第一代"内容
  const detailPrevGen: SegmentDetail = {
    ...SEGMENTS_TWO_GENS[1],
    members: [
      {
        id: 'mc-gen1-user',
        thread_id: 'sess-1',
        sequence: 50,
        role: 'user',
        content: '第一代问题',
        timestamp: '2026-09-23T00:00:01Z',
        status: 'completed',
      },
      {
        id: 'mc-gen1-assistant',
        thread_id: 'sess-1',
        role: 'assistant',
        content: '第一代回答',
        timestamp: '2026-09-23T00:00:02Z',
        status: 'completed',
      },
    ],
  }

  it('‹ 从启用序列切上一代：段详情换装 + segment_activate 出站（乐观态落地）', async () => {
    getMessageSegmentDetailMock.mockResolvedValue(detailPrevGen)
    render(
      <MessageActions
        message={makeMessage('assistant', 51)}
        sessionId="sess-1"
        segmentSwitcherBaseSeq={50}
      />,
    )
    fireEvent.click(screen.getByTestId('segment-prev'))

    await waitFor(() => {
      expect(sendSegmentActivateMock).toHaveBeenCalledTimes(1)
    })
    expect(getMessageSegmentDetailMock).toHaveBeenCalledWith('seg-gen2')
    expect(sendSegmentActivateMock).toHaveBeenCalledWith('sess-1', {
      pipelineId: 'pipe-1',
      segmentId: 'seg-gen2',
    })
    // 乐观换装：锚点后的视图已被段成员覆写，段视图标记落地
    const msgs = usePipelineMessageStore.getState().getMessages('pipe-1')
    expect(msgs.map((m) => m.id)).toEqual(['msg-keep', 'mc-gen1-user', 'mc-gen1-assistant'])
    expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-1']).toEqual({
      baseSeq: 50,
      segmentId: 'seg-gen2',
    })
  })

  it('段详情拉取失败 → toast 且不出站、不换装（不改启用状态）', async () => {
    getMessageSegmentDetailMock.mockRejectedValue(new Error('boom'))
    render(
      <MessageActions
        message={makeMessage('assistant', 51)}
        sessionId="sess-1"
        segmentSwitcherBaseSeq={50}
      />,
    )
    fireEvent.click(screen.getByTestId('segment-prev'))

    await waitFor(() => {
      expect(toastErrorMock).toHaveBeenCalledWith('获取对话版本失败，请稍后重试')
    })
    expect(sendSegmentActivateMock).not.toHaveBeenCalled()
    expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-1']).toBeUndefined()
    expect(usePipelineMessageStore.getState().getMessages('pipe-1').map((m) => m.id)).toEqual([
      'msg-keep',
      'msg-50',
      'msg-51',
    ])
  })
})

describe('运行中拒绝（服务端约束 §7.3：run 活跃时激活必拒）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    seedStore({ streaming: true })
  })

  it('流式运行中点击切换 → toast「任务运行中」，不换装不出站', () => {
    render(
      <MessageActions
        message={makeMessage('assistant', 51)}
        sessionId="sess-1"
        segmentSwitcherBaseSeq={50}
      />,
    )
    fireEvent.click(screen.getByTestId('segment-prev'))
    fireEvent.click(screen.getByTestId('segment-next'))

    expect(toastErrorMock).toHaveBeenCalledWith('任务运行中，无法切换对话版本')
    expect(getMessageSegmentDetailMock).not.toHaveBeenCalled()
    expect(sendSegmentActivateMock).not.toHaveBeenCalled()
    expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-1']).toBeUndefined()
    expect(usePipelineMessageStore.getState().getMessages('pipe-1').map((m) => m.id)).toEqual([
      'msg-keep',
      'msg-50',
      'msg-51',
    ])
  })

  it('disabled（生成态透传）时同样拒绝且不改启用状态', () => {
    seedStore()
    render(
      <MessageActions
        message={makeMessage('assistant', 51)}
        sessionId="sess-1"
        disabled
        segmentSwitcherBaseSeq={50}
      />,
    )
    fireEvent.click(screen.getByTestId('segment-prev'))
    expect(toastErrorMock).toHaveBeenCalledWith('任务运行中，无法切换对话版本')
    expect(getMessageSegmentDetailMock).not.toHaveBeenCalled()
  })
})
