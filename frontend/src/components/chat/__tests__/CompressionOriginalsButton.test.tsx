// @feature: FP-T12 前端适配(压缩原文按钮) | @ci: frontend-test
// @feature: message-segments P0 | @ci: frontend-test
/**
 * CompressionOriginalsButton — 宿主侧「查看原始 N 条」桥接（消息段模型 §5.1
 * 展开双层②原始消息）。
 *
 * 背景：卡上行桥只放行 /ext/{pluginId}/**（web/cards/compression.html 取数
 * 缺口），段取数由宿主承担——点击 fetch GET message-segments/{id}，Modal 只读
 * 渲染 members（不激活、不写序列）。计数口径与卡一致：seq_range 跨度优先。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const toastErrorMock = vi.fn()
vi.mock('sonner', () => ({
  toast: { error: (...args: unknown[]) => toastErrorMock(...args), success: vi.fn() },
}))

const getMessageSegmentDetailMock = vi.fn()
vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegmentDetail: (...args: unknown[]) => getMessageSegmentDetailMock(...args),
}))

import { CompressionOriginalsButton, readCompressionRef } from '../CompressionOriginalsButton'
import type { Message } from '@/types/models'

function makeBlockMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'mc-block',
    sessionId: 'sess-1',
    sequence: 12,
    role: 'system',
    content: '<compressed seq="40-55">…</compressed>',
    timestamp: new Date().toISOString(),
    status: 'completed',
    metadata: {
      name: 'compressed',
      message_style: 'compression_card',
      compression_ref: { segment_id: 'seg-block-1', seq_range: [40, 55] },
    },
    ...overrides,
  } as Message
}

describe('readCompressionRef', () => {
  it('仅 system 块消息携带 segment_id 时返回引用；计数 = seq_range 跨度', () => {
    expect(readCompressionRef(makeBlockMessage())).toEqual({ segmentId: 'seg-block-1', count: 16 })
    // 非 system（assistant 带 compression_ref）不出宿主入口
    expect(readCompressionRef(makeBlockMessage({ role: 'assistant' }))).toBeNull()
    // 无 segment_id（数据未注入）不出入口
    expect(
      readCompressionRef(makeBlockMessage({ metadata: { compression_ref: { seq_range: [1, 2] } } })),
    ).toBeNull()
    // seq_range 缺失 → count 0（按钮退化为「查看原始」）
    expect(
      readCompressionRef(
        makeBlockMessage({ metadata: { compression_ref: { segment_id: 'seg-9' } } }),
      ),
    ).toEqual({ segmentId: 'seg-9', count: 0 })
  })
})

describe('CompressionOriginalsButton — 宿主桥接', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('点击取段详情 → Modal 只读渲染成员消息列表（role/时间 + 正文）', async () => {
    getMessageSegmentDetailMock.mockResolvedValue({
      id: 'seg-block-1',
      base_seq: 40,
      base_len: 2,
      visible_to: '',
      preview: '…',
      created_at: '2026-09-23T00:00:00Z',
      members: [
        { id: 'mc-1', role: 'user', content: '第一代问题', timestamp: '2026-09-23T00:00:01Z' },
        { id: 'mc-2', role: 'assistant', content: '第一代回答', timestamp: '2026-09-23T00:00:02Z' },
      ],
    })

    render(<CompressionOriginalsButton message={makeBlockMessage()} />)
    fireEvent.click(screen.getByTestId('compression-originals-button'))

    expect(getMessageSegmentDetailMock).toHaveBeenCalledWith('seg-block-1')
    const list = await screen.findByTestId('originals-list')
    const items = list.querySelectorAll('[data-testid="original-message"]')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveAttribute('data-role', 'user')
    expect(items[1]).toHaveAttribute('data-role', 'assistant')
    expect(screen.getByText('第一代问题')).toBeInTheDocument()
    expect(screen.getByText('第一代回答')).toBeInTheDocument()
  })

  it('取数失败 → toast 且 Modal 不出列表；重开可重试取数', async () => {
    getMessageSegmentDetailMock.mockRejectedValueOnce(new Error('network down'))
    getMessageSegmentDetailMock.mockResolvedValueOnce({
      id: 'seg-block-1',
      base_seq: 40,
      base_len: 1,
      visible_to: '',
      preview: '…',
      created_at: '2026-09-23T00:00:00Z',
      members: [{ id: 'mc-1', role: 'user', content: '第一代问题', timestamp: '2026-09-23T00:00:01Z' }],
    })

    render(<CompressionOriginalsButton message={makeBlockMessage()} />)
    fireEvent.click(screen.getByTestId('compression-originals-button'))
    await waitFor(() => {
      expect(toastErrorMock).toHaveBeenCalledWith('获取原始消息失败，请稍后重试')
    })
    await waitFor(() => {
      expect(screen.queryByTestId('originals-list')).not.toBeInTheDocument()
    })

    // Modal 关闭态重开（失败后 detail 未缓存）→ 重新取数成功
    fireEvent.click(screen.getByTestId('compression-originals-button'))
    await screen.findByTestId('originals-list')
    expect(getMessageSegmentDetailMock).toHaveBeenCalledTimes(2)
  })

  it('无 compression_ref（或非 system）→ 不渲染入口（零残留）', () => {
    const { container: c1 } = render(
      <CompressionOriginalsButton
        message={makeBlockMessage({ metadata: { message_style: 'compression_card' } })}
      />,
    )
    expect(c1.querySelectorAll('[data-testid="compression-originals-button"]')).toHaveLength(0)
    const { container: c2 } = render(
      <CompressionOriginalsButton message={makeBlockMessage({ role: 'assistant' })} />,
    )
    expect(c2.querySelectorAll('[data-testid="compression-originals-button"]')).toHaveLength(0)
  })
})
