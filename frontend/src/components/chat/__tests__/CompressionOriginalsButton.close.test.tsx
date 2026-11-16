/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 「查看原始 N 条」弹窗的关闭路径补测：点弹窗关闭键收起弹窗（原始消息是只读
 * 预览，关闭即回到卡片，不改变任何状态）。
 *
 * mock 边界：段详情 HTTP（只读数据源）、toast。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', () => ({ toast: { error: vi.fn(), success: vi.fn() } }))

const getMessageSegmentDetailMock = vi.fn()
vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegmentDetail: (...args: unknown[]) => getMessageSegmentDetailMock(...args),
}))

import { CompressionOriginalsButton } from '../CompressionOriginalsButton'
import type { Message } from '@/types/models'

function makeBlockMessage(): Message {
  return {
    id: 'mc-block',
    sessionId: 'sess-1',
    sequence: 12,
    role: 'system',
    content: '<compressed seq="40-55">…</compressed>',
    timestamp: '2026-10-28T00:00:00Z',
    status: 'completed',
    metadata: {
      name: 'compressed',
      message_style: 'compression_card',
      compression_ref: { segment_id: 'seg-block-1', seq_range: [40, 55] },
    },
  } as Message
}

beforeEach(() => {
  vi.clearAllMocks()
  getMessageSegmentDetailMock.mockResolvedValue({
    id: 'seg-block-1',
    base_seq: 40,
    base_len: 16,
    visible_to: '',
    preview: null,
    created_at: '2026-10-28T00:00:00Z',
    members: [
      {
        id: 'm-1',
        thread_id: 'sess-1',
        sequence: 40,
        role: 'user',
        content: '原始提问',
        timestamp: '2026-10-28T00:00:01Z',
        status: 'completed',
      },
    ],
  })
})

describe('CompressionOriginalsButton — 弹窗关闭', () => {
  it('打开弹窗后点关闭键 → 弹窗收起（只读预览不改状态）', async () => {
    const { container } = render(<CompressionOriginalsButton message={makeBlockMessage()} />)

    fireEvent.click(screen.getByText(/查看原始/))
    await waitFor(() => expect(screen.getByText('原始提问')).toBeInTheDocument())

    const closeBtn = container.ownerDocument.querySelector('.ant-modal-close') as HTMLElement
    expect(closeBtn).not.toBeNull()
    fireEvent.click(closeBtn)

    // 关闭后弹窗容器隐藏（antd 关闭动效结束后置 display:none，内容随之不可见）
    // 关闭态可观察信号：dialog 进入离场（关闭动效类）或被卸载——两者都表示
    // 预览已退出可见交互面
    const dialog = container.ownerDocument.querySelector('[role="dialog"]') as HTMLElement | null
    await waitFor(() => {
      const leaving = !dialog || !dialog.isConnected || dialog.className.includes('ant-zoom-leave')
      expect(leaving).toBe(true)
    })
  })
})
