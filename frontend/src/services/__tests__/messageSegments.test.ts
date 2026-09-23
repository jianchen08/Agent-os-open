// @feature: FP-0.2.四 前端Schema(消息段模型) | @ci: frontend-test
// @feature: message-segments P1 | @ci: frontend-test
/**
 * 消息段 API 层信封解析（消息段模型 §5 读路径）。
 *
 * 契约与内核双形态并存（[来源: docs/working/消息历史双能力方案_多代切换与压缩原文_20260923.md §5]）：
 * - 清单：契约 `{segments:[...]}`；内核 routes.rs 清单族惯例 `{items:[...]}`；
 * - 详情：契约 `{segment:{...members}}`；内核将段对象置顶（members 在顶层）；
 * - 成员词汇：槽位 blob 存储（message_id/seq）与 HTTP 投影（id/sequence）归一；
 * - 都缺 = 协议违反 fail-closed 抛错（不降级空表，防切换器静默消失）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))

import { getMessageSegmentDetail, getMessageSegments } from '@/services/api/messageSegments'

const SEG = {
  id: 'seg_abc',
  base_seq: 50,
  base_len: 2,
  visible_to: '',
  preview: '第一代',
  created_at: '2026-09-23T00:00:00Z',
}

describe('getMessageSegments 信封解析', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('契约形态 {segments:[...]} 解析为段列表', async () => {
    mockGet.mockResolvedValue({ data: { segments: [SEG] } })
    const list = await getMessageSegments('pipe-1')
    expect(mockGet).toHaveBeenCalledWith('/api/v1/pipelines/pipe-1/message-segments')
    expect(list).toHaveLength(1)
    expect(list[0]).toMatchObject({ id: 'seg_abc', base_seq: 50 })
  })

  it('内核形态 {items:[...]} 同样解析（routes.rs 清单族惯例）', async () => {
    mockGet.mockResolvedValue({ data: { items: [{ ...SEG, id: 'seg_items' }] } })
    const list = await getMessageSegments('pipe-1')
    expect(list[0]?.id).toBe('seg_items')
  })

  it('契约形态优先（两键并存时取 segments）', async () => {
    mockGet.mockResolvedValue({
      data: { segments: [{ ...SEG, id: 'seg_contract' }], items: [{ ...SEG, id: 'seg_items' }] },
    })
    const list = await getMessageSegments('pipe-1')
    expect(list[0]?.id).toBe('seg_contract')
  })

  it('两键都缺 = 协议违反抛错（不静默降级空表）', async () => {
    mockGet.mockResolvedValue({ data: {} })
    await expect(getMessageSegments('pipe-1')).rejects.toThrow('协议违反')
  })
})

describe('getMessageSegmentDetail 信封解析', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('契约形态 {segment:{...members}} 解析并补齐成员身份键', async () => {
    mockGet.mockResolvedValue({
      data: {
        segment: {
          ...SEG,
          members: [
            { id: 'mc-1', role: 'user', content: '第一代', sequence: 50, timestamp: '2026-09-23T00:00:01Z' },
          ],
        },
      },
    })
    const detail = await getMessageSegmentDetail('seg_abc')
    expect(detail.members[0]?.id).toBe('mc-1')
    expect(detail.members[0]?.sequence).toBe(50)
  })

  it('内核形态（段对象置顶、members 在顶层）同样解析', async () => {
    mockGet.mockResolvedValue({
      data: {
        ...SEG,
        members: [{ message_id: 'mc-2', role: 'assistant', content: '第一代回答', seq: 51 }],
      },
    })
    const detail = await getMessageSegmentDetail('seg_abc')
    // 槽位 blob 词汇 message_id/seq → 前端消费键 id/sequence
    expect(detail.members[0]?.id).toBe('mc-2')
    expect(detail.members[0]?.sequence).toBe(51)
  })

  it('members 缺失 = 协议违反抛错', async () => {
    mockGet.mockResolvedValue({ data: { ...SEG } })
    await expect(getMessageSegmentDetail('seg_abc')).rejects.toThrow('协议违反')
  })
})
