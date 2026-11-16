/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * ‹i/n› 代际切换器残余分支：
 * - 切到上一代后「›」可用 → 点击按目标序位换装并出站（正常前进路径）
 * - 段详情在途期间管道开始流式运行 → 落地前二次判定拒绝（toast、不换装、不出站）
 *
 * 断行为：段视图标记/出站帧/提示文案；mock 边界仅外部依赖（段详情 HTTP、
 * WS 出站、toast）。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
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
vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegments: vi.fn().mockResolvedValue([]),
  getMessageSegmentDetail: (...args: unknown[]) => getMessageSegmentDetailMock(...args),
}))

function makeSegment(id: string, baseSeq: number, createdAt: string): SegmentMeta {
  return { id, base_seq: baseSeq, base_len: 2, visible_to: '', preview: null, created_at: createdAt }
}

function makeMessage(role: Message['role'], sequence: number): Message {
  return {
    id: `msg-${sequence}`,
    sessionId: 'sess-1',
    sequence,
    role,
    content: '内容',
    timestamp: '2026-10-28T00:00:00Z',
    parentId: null,
    status: 'completed',
  } as Message
}

// 三代同锚点：‹ 从「当前」跳到上一代（末代）后序号落在中间档 → › 可用
const SEGMENTS_THREE_GENS = [
  makeSegment('seg-gen1', 50, '2026-10-28T00:00:00Z'),
  makeSegment('seg-gen2', 50, '2026-10-28T02:00:00Z'),
  makeSegment('seg-gen3', 50, '2026-10-28T04:00:00Z'),
]

const detailPrevGen: SegmentDetail = {
  ...SEGMENTS_THREE_GENS[1],
  members: [
    {
      id: 'mc-gen1-user',
      thread_id: 'sess-1',
      sequence: 50,
      role: 'user',
      content: '第一代问题',
      timestamp: '2026-10-28T00:00:01Z',
      status: 'completed',
    },
  ],
}

function seedStore(streaming = false) {
  usePipelineMessageStore.setState({
    activePipelineId: 'pipe-1',
    pipelineSessionMap: { 'pipe-1': 'sess-1' },
    spanIndex: { 'pipe-1': SEGMENTS_THREE_GENS },
    segmentViewByPipeline: {},
    streamingState: streaming
      ? { 'pipe-1': { isStreaming: true, messageId: 'm-live', startedAt: Date.now() } as never }
      : {},
    messagesByPipeline: {
      'pipe-1': [makeMessage('user', 49), makeMessage('user', 50), makeMessage('assistant', 51)],
    },
  } as never)
}

function renderSwitcher() {
  return render(
    <MessageActions
      message={makeMessage('assistant', 51)}
      sessionId="sess-1"
      segmentSwitcherBaseSeq={50}
    />,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  seedStore()
  getMessageSegmentDetailMock.mockResolvedValue(detailPrevGen)
})

describe('代际切换器 — 前进路径', () => {
  it('切到上一代后「›」可用，点击按目标序位换装并出站', async () => {
    renderSwitcher()

    fireEvent.click(screen.getByTestId('segment-prev'))
    await waitFor(() => expect(sendSegmentActivateMock).toHaveBeenCalledTimes(1))

    const next = screen.getByTestId('segment-next')
    expect(next).not.toBeDisabled()
    fireEvent.click(next)

    await waitFor(() => expect(getMessageSegmentDetailMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(sendSegmentActivateMock).toHaveBeenCalledTimes(2))
  })
})

describe('代际切换器 — 在途期间开始运行', () => {
  it('段详情返回时管道已流式运行 → 拒绝落地（toast、不换装、不出站）', async () => {
    let resolveDetail: ((d: SegmentDetail) => void) | null = null
    getMessageSegmentDetailMock.mockImplementation(
      () => new Promise<SegmentDetail>((resolve) => {
        resolveDetail = resolve
      }),
    )
    renderSwitcher()

    fireEvent.click(screen.getByTestId('segment-prev'))
    // 详情在途：此时管道开始流式输出
    usePipelineMessageStore.setState({
      streamingState: {
        'pipe-1': { isStreaming: true, messageId: 'm-live', startedAt: Date.now() } as never,
      },
    } as never)
    resolveDetail?.(detailPrevGen)

    await waitFor(() =>
      expect(toastErrorMock).toHaveBeenCalledWith('任务运行中，无法切换对话版本'),
    )
    expect(sendSegmentActivateMock).not.toHaveBeenCalled()
    expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-1']).toBeUndefined()
  })
})
