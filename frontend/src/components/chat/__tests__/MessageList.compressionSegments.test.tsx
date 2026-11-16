/**
 * MessageList 压缩段归组渲染测试（消息段模型 §5.1 展示层口径）
 *
 * 验证 2026-10-17 用户裁定的列表级行为：
 * - 同段两块（过程块+快照块）只渲染一支插件压缩卡（归组，不再两卡成对）
 * - 卡面 = PluginMessageCard fit=auto（渲染本体归插件）；同段块内容由宿主
 *   合并为一份下行载荷（组卡性质断言：一份载荷同时含两块的内联标签）
 * - 段成员原文装配在卡下方（与压缩前显示一致，只读行）
 * - 声明缺失（registry 无此样式）整组回退普通渲染，块不消失
 * - 普通消息不受影响
 */

import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { getMessageSegmentDetail } from '@/services/api/messageSegments'
import { MessageList } from '../MessageList'
import type { ExtendedMessageListProps } from '../MessageList'
import type { SegmentDetail } from '@/services/api/messageSegments'
import type { Message } from '@/types/models'

vi.mock('../MessageItem', () => ({
  MessageItem: ({ message }: { message: Message }) => (
    <div data-testid={`message-item-${message.id}`}>{message.content}</div>
  ),
}))

vi.mock('../PluginMessageCard', () => ({
  MESSAGE_STYLE_METADATA_KEY: 'message_style',
  PluginMessageCard: ({
    instanceKey,
    styleId,
    message,
    fit,
  }: {
    instanceKey: string
    styleId: string
    message?: { content: string; metadata?: Record<string, unknown> | null }
    fit?: string
  }) => (
    <div data-testid="plugin-message-card" data-fit={fit} data-style={styleId} data-instance={instanceKey}>
      {message?.content}
    </div>
  ),
  resolveMessageStyle: (styleId: unknown) =>
    styleId === 'compression_card' ? { page: { title: '压缩卡' }, pluginId: 'p' } : null,
}))

vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegmentDetail: vi.fn(),
}))

const mockGetDetail = vi.mocked(getMessageSegmentDetail)

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-001',
    sessionId: 'sess-1',
    sequence: 1,
    role: 'assistant',
    content: '测试消息内容',
    timestamp: '2026-10-16T17:20:00Z',
    status: 'completed',
    ...overrides,
  }
}

function makeBlock(id: string, seq: number, content: string, styleId = 'compression_card'): Message {
  return {
    ...makeMessage({ id, sequence: seq, role: 'system', content }),
    metadata: {
      message_style: styleId,
      compression_ref: { segment_id: 'seg_x', seq_range: [2, 3] },
    },
  }
}

function makeDetail(): SegmentDetail {
  return {
    id: 'seg_x',
    base_seq: 2,
    base_len: 2,
    visible_to: 'user',
    preview: null,
    created_at: '2026-10-16T17:25:34Z',
    members: [
      {
        id: 'orig-user',
        role: 'user',
        content: '原始提问内容',
        sequence: 2,
        timestamp: '2026-10-16T17:10:00Z',
      },
      {
        id: 'orig-assistant',
        role: 'assistant',
        content: '原始回复内容',
        sequence: 3,
        timestamp: '2026-10-16T17:11:00Z',
      },
    ] as SegmentDetail['members'],
  }
}

function renderList(messages: Message[], props: Partial<ExtendedMessageListProps> = {}) {
  return render(<MessageList messages={messages} {...props} />)
}

beforeEach(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
  mockGetDetail.mockResolvedValue(makeDetail())
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('MessageList 压缩段归组（插件卡面）', () => {
  it('同段两块只渲染一支插件卡（fit=auto），块不再单独成行', async () => {
    renderList([
      makeMessage({ id: 'head', sequence: 1, role: 'user', content: '开场提问' }),
      makeBlock('blk-a', 2, '<compressed seq="2-3" level="L1">摘要正文</compressed>'),
      makeBlock('blk-b', 3, '<current_state>{"goal":"调研"}</current_state>'),
      makeMessage({ id: 'tail', sequence: 5, content: '后续回复' }),
    ])

    const cards = screen.getAllByTestId('plugin-message-card')
    expect(cards).toHaveLength(1)
    expect(cards[0]).toHaveAttribute('data-fit', 'auto')
    expect(cards[0]).toHaveAttribute('data-style', 'compression_card')
    // 块消息由一支卡代表，不再各自成行
    expect(screen.queryByTestId('message-item-blk-a')).toBeNull()
    expect(screen.queryByTestId('message-item-blk-b')).toBeNull()
    // 普通消息不受影响
    expect(screen.getByTestId('message-item-head')).toBeInTheDocument()
    expect(screen.getByTestId('message-item-tail')).toBeInTheDocument()
  })

  it('组卡下行载荷为同段块内容合并（一份载荷同时含过程与快照标签）', async () => {
    renderList([
      makeBlock('blk-a', 2, '<compressed seq="2-3" level="L1">摘要正文</compressed>'),
      makeBlock('blk-b', 3, '<current_state>{"goal":"调研"}</current_state>'),
    ])
    const card = screen.getByTestId('plugin-message-card')
    expect(card).toHaveTextContent('<compressed seq="2-3" level="L1">摘要正文</compressed>')
    expect(card).toHaveTextContent('<current_state>{"goal":"调研"}</current_state>')
    // 分界落位（1017 用户裁定）：原文在上、压缩分界卡在下——卡在原文容器之后
    await waitFor(() => {
      expect(screen.getByTestId('message-item-orig-user')).toBeInTheDocument()
    })
    const originals = screen.getByTestId('compression-segment-originals')
    expect(originals.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(mockGetDetail).toHaveBeenCalledWith('seg_x')
  })

  it('多段各自归组：三段渲染三支卡，原文各随其后', async () => {
    const block = (id: string, seq: number, seg: string) => ({
      ...makeMessage({ id, sequence: seq, role: 'system', content: `<compressed seq="${seq}-${seq}">摘要</compressed>` }),
      metadata: {
        message_style: 'compression_card',
        compression_ref: { segment_id: seg, seq_range: [seq, seq] },
      },
    })
    renderList([
      block('b1', 2, 'seg_1'),
      block('b2', 4, 'seg_2'),
      block('b3', 6, 'seg_3'),
      makeMessage({ id: 'tail', sequence: 8, content: '未压缩消息' }),
    ])
    expect(screen.getAllByTestId('plugin-message-card')).toHaveLength(3)
    await waitFor(() => expect(screen.getAllByTestId('compression-segment-originals')).toHaveLength(3))
  })

  it('声明缺失（registry 无此样式）：整组回退普通渲染，块不消失', () => {
    renderList([
      makeBlock('blk-unknown', 2, '<compressed seq="2-2">摘要</compressed>', 'unknown_style'),
    ])
    expect(screen.queryByTestId('plugin-message-card')).toBeNull()
    expect(screen.getByTestId('message-item-blk-unknown')).toBeInTheDocument()
  })

  it('段详情不可用时列表照常渲染（卡面仍在，原文区提示失败）', async () => {
    mockGetDetail.mockRejectedValue(new Error('404'))
    renderList([makeBlock('blk-a', 2, '<compressed seq="2-3">摘要</compressed>')])
    expect(screen.getByTestId('plugin-message-card')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('原始消息加载失败')).toBeInTheDocument())
  })
})
