/**
 * SegmentOriginals 装配器测试（消息段模型 §5.1 展示层，宿主数据路由面）
 *
 * 验证：按 segment_id 取段详情 → 成员映射+主列表同款流水线 → 只读行挂载；
 * 取数失败给一行提示不阻塞。卡面（渲染本体）归插件 compression_card，
 * 本组件零卡面 UI——断言不含任何卡面交互。
 *
 * 防拟合：成功/失败两组有区分度输入；取数坐标断言（segmentId 直传）。
 */

import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { getMessageSegmentDetail } from '@/services/api/messageSegments'
import { SegmentOriginals } from '../SegmentOriginals'
import type { SegmentDetail } from '@/services/api/messageSegments'
import type { Message } from '@/types/models'

// mock 传输层而非 messageSegments 模块本体——成员词汇归一在真实
// getMessageSegmentDetail 内，mock 模块会把它一并旁路掉（工具词汇
// 投影路径就测不到了）。
vi.mock('@/services/api/client', () => ({
  default: { get: vi.fn() },
}))

import apiClient from '@/services/api/client'
const mockGet = vi.mocked(apiClient.get)

/** 段详情信封（真实 getMessageSegmentDetail 消费 {data:{segment}} 形态） */
function mockSegmentEnvelope(detail: SegmentDetail | null) {
  mockGet.mockResolvedValue({ data: detail ? { segment: detail } : {} } as never)
}

function makeDetail(): SegmentDetail {
  return {
    id: 'seg_1',
    base_seq: 3,
    base_len: 2,
    visible_to: 'user',
    preview: null,
    created_at: '2026-10-16T17:25:34Z',
    members: [
      {
        id: 'm1',
        role: 'user',
        content: '原始提问内容',
        sequence: 3,
        timestamp: '2026-10-16T17:20:00Z',
      },
      {
        id: 'm2',
        role: 'assistant',
        content: '原始回复内容',
        sequence: 4,
        timestamp: '2026-10-16T17:21:00Z',
      },
    ] as SegmentDetail['members'],
  }
}

const renderMember = (m: Message) => <div data-testid={`member-${m.id}`}>{m.content}</div>

beforeEach(() => {
  vi.clearAllMocks()
  mockSegmentEnvelope(makeDetail())
})

describe('SegmentOriginals 装配', () => {
  it('按 segment_id 取段详情，成员经映射后原样还原为消息行', async () => {
    render(<SegmentOriginals segmentId="seg_1" sessionId="sess-1" renderMember={renderMember} />)
    expect(mockGet).toHaveBeenCalledWith('/api/v1/message-segments/seg_1')
    expect(await screen.findByTestId('member-m1')).toHaveTextContent('原始提问内容')
    expect(screen.getByTestId('member-m2')).toHaveTextContent('原始回复内容')
  })

  it('取数失败：给一行失败提示，不渲染成员、不上抛', async () => {
    mockGet.mockRejectedValue(new Error('network down'))
    render(<SegmentOriginals segmentId="seg_1" sessionId="sess-1" renderMember={renderMember} />)
    expect(await screen.findByText('原始消息加载失败')).toBeInTheDocument()
    expect(screen.queryByTestId('member-m1')).toBeNull()
  })

  it('segmentId 为空：不发起取数（fail-closed，不猜坐标）', async () => {
    render(<SegmentOriginals segmentId="" sessionId="sess-1" renderMember={renderMember} />)
    expect(mockGet).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByText('原始消息加载失败')).toBeNull())
  })

  it('冻结成员为原始 LLM 工具词汇时照常吸收：tool 结果并回 assistant 活动卡，无孤行工具条', async () => {
    // 冻结 blob 直读形态：assistant 带 tool_calls（原始 LLM 数组）、tool 带
    // tool_call_id——不投影则吸收断链，工具消息只剩一个「工具」标签孤行。
    mockSegmentEnvelope({
      ...makeDetail(),
      members: [
        {
          id: 'm-assistant',
          role: 'assistant',
          content: '',
          sequence: 3,
          timestamp: '2026-10-16T17:20:00Z',
          tool_calls: [
            { id: 'call_1', type: 'function', function: { name: 'read_file', arguments: '{"path":"a.json"}' } },
          ],
        },
        {
          id: 'm-tool',
          role: 'tool',
          content: '文件内容全文',
          sequence: 4,
          timestamp: '2026-10-16T17:20:01Z',
          tool_call_id: 'call_1',
        },
      ] as SegmentDetail['members'],
    })
    const seen: Message[] = []
    render(
      <SegmentOriginals
        segmentId="seg_1"
        sessionId="sess-1"
        renderMember={(m) => {
          seen.push(m)
          return <div data-testid={`member-${m.id}`}>{m.id}</div>
        }}
      />,
    )
    await waitFor(() => expect(screen.getByTestId('member-m-assistant')).toBeInTheDocument())
    // tool 成员被吸收：不独立成行（防拟合：吸收前后条数 2→1 有区分度）
    expect(seen).toHaveLength(1)
    expect(seen[0].role).toBe('assistant')
    // 结果注入进 assistant 的 tool_call part（callId 对上、result 在场）
    const tcPart = seen[0].parts?.find((p) => p.type === 'tool_call') as
      | { callId?: string; result?: unknown }
      | undefined
    expect(tcPart?.callId).toBe('call_1')
    expect(tcPart?.result).toBeTruthy()
    expect(screen.queryByTestId('member-m-tool')).toBeNull()
  })
})
