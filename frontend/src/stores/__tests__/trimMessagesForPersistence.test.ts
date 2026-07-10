/**
 * trimMessagesForPersistence 裁剪策略测试
 *
 * 验证两个维度：
 * 1. 单管道按条数裁剪（默认 250 条，超出取最新 N 条）；
 * 2. 全局总体积超 PERSIST_MAX_TOTAL_BYTES 时按 LRU 淘汰最不活跃管道（活跃管道始终保留）。
 *
 * 内存数据不受影响（本函数只控制落盘裁剪）。
 */
import { describe, it, expect } from 'vitest'
import { trimMessagesForPersistence, _PERSIST_LIMITS } from '@/stores/pipelineMessageStore'
import type { Message } from '@/types/models'

const makeMsg = (id: string, seq: number, ts: string, content = 'x'): Message => ({
  id,
  sessionId: 'sess',
  sequence: seq,
  role: 'assistant',
  content,
  timestamp: ts,
  parentId: null,
  status: 'completed',
})

describe('trimMessagesForPersistence', () => {
  it('单管道超过 250 条时仅保留最新 250 条', () => {
    const LIMIT = _PERSIST_LIMITS.maxMessagesPerPipeline
    expect(LIMIT).toBe(250)
    const msgs: Message[] = []
    // 构造 300 条，sequence 1..300
    for (let i = 1; i <= 300; i++) {
      msgs.push(makeMsg(`m${i}`, i, `2026-01-01T00:00:0${i % 10}Z`))
    }
    const result = trimMessagesForPersistence({ p1: msgs }, null)
    expect(result.p1).toHaveLength(LIMIT)
    // 保留最新的 250 条：sequence 51..300（compareMessages 按 sequence 升序后取最后 250）
    expect(result.p1[0].sequence).toBe(51)
    expect(result.p1[LIMIT - 1].sequence).toBe(300)
  })

  it('单管道不超过 250 条时全部保留', () => {
    const msgs = [makeMsg('a', 1, '2026-01-01T00:00:00Z'), makeMsg('b', 2, '2026-01-01T00:00:01Z')]
    const result = trimMessagesForPersistence({ p1: msgs }, null)
    expect(result.p1).toHaveLength(2)
  })

  it('空管道和缺失管道被跳过', () => {
    const result = trimMessagesForPersistence({ p1: [], p2: undefined as any }, null)
    expect(result.p1).toBeUndefined()
    expect(result.p2).toBeUndefined()
  })

  it('体积未超阈值时全部保留（不淘汰）', () => {
    // 构造小体积多管道，远低于 100MB
    const mp: Record<string, Message[]> = {}
    for (let i = 0; i < 10; i++) {
      mp[`pipe-${i}`] = [makeMsg(`m${i}`, 1, `2026-01-0${i + 1}T00:00:00Z`)]
    }
    const result = trimMessagesForPersistence(mp, null)
    expect(Object.keys(result)).toHaveLength(10)
  })

  it('体积超阈值时按 LRU 淘汰最不活跃管道，活跃管道始终保留', () => {
    // 构造 4 个管道，总体积超过阈值。活跃管道 pipe-active 体积最大但必须保留；
    // 其余按 timestamp 排序，最旧的优先淘汰，直到体积达标。
    // 用大 content 制造体积：每个管道约 40MB，4 个 = 160MB > 100MB
    const big40 = 'A'.repeat(40 * 1024 * 1024)
    // pipe-old：最旧，应被淘汰
    // pipe-mid：中等，可能保留
    // pipe-new：最新
    // pipe-active：活跃管道（体积也大），必须保留
    const mp: Record<string, Message[]> = {
      'pipe-old': [makeMsg('o1', 1, '2025-01-01T00:00:00Z', big40)], // 最旧 → 淘汰
      'pipe-mid': [makeMsg('mi1', 1, '2025-06-01T00:00:00Z', big40)],
      'pipe-new': [makeMsg('n1', 1, '2025-12-01T00:00:00Z', big40)],
      'pipe-active': [makeMsg('a1', 1, '2025-03-01T00:00:00Z', big40)], // 活跃，时间旧但强制保留
    }

    const result = trimMessagesForPersistence(mp, 'pipe-active')

    // 活跃管道必须保留
    expect(result['pipe-active']).toBeDefined()
    // 最旧的 pipe-old 应被淘汰（活跃管道强制保留占名额后，剩余额度不够再留最旧的）
    expect(result['pipe-old']).toBeUndefined()
    // 总体积必须降到阈值内
    let totalBytes = 0
    for (const msgs of Object.values(result)) {
      totalBytes += JSON.stringify(msgs).length
    }
    expect(totalBytes).toBeLessThanOrEqual(_PERSIST_LIMITS.maxTotalBytes)
  })

  it('活跃管道无论体积多大都保留', () => {
    // 单个活跃管道就超阈值（120MB > 100MB）
    const big120 = 'B'.repeat(120 * 1024 * 1024)
    const mp = {
      'pipe-active': [makeMsg('a1', 1, '2026-01-01T00:00:00Z', big120)],
    }
    const result = trimMessagesForPersistence(mp, 'pipe-active')
    // 唯一的活跃管道不应被淘汰
    expect(result['pipe-active']).toBeDefined()
    expect(result['pipe-active']).toHaveLength(1)
  })

  it('无活跃管道时，纯按最近活跃时间淘汰最旧的', () => {
    const big40 = 'C'.repeat(40 * 1024 * 1024)
    const mp: Record<string, Message[]> = {
      'pipe-1': [makeMsg('p1', 1, '2025-01-01T00:00:00Z', big40)], // 最旧
      'pipe-2': [makeMsg('p2', 1, '2025-06-01T00:00:00Z', big40)],
      'pipe-3': [makeMsg('p3', 1, '2025-12-01T00:00:00Z', big40)], // 最新
    }
    const result = trimMessagesForPersistence(mp, null)
    // pipe-1 最旧应被淘汰，pipe-3 最新保留，pipe-2 视额度
    expect(result['pipe-1']).toBeUndefined()
    expect(result['pipe-3']).toBeDefined()
    let totalBytes = 0
    for (const msgs of Object.values(result)) {
      totalBytes += JSON.stringify(msgs).length
    }
    expect(totalBytes).toBeLessThanOrEqual(_PERSIST_LIMITS.maxTotalBytes)
  })
})
