// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** 全局 WS URL 构建：断线补漏参数 last_sequence 追加（重连补放语义） */
import { describe, expect, it } from 'vitest'
import { buildGlobalWebSocketUrl } from '@/constants/websocket'

describe('buildGlobalWebSocketUrl', () => {
  it('带 last_sequence → URL 追加 &last_sequence=N（断线补漏）', () => {
    const url = buildGlobalWebSocketUrl({ ticket: 't-1' } as never, 42)
    expect(url).toContain('ticket=t-1')
    expect(url).toContain('&last_sequence=42')
  })

  it('未带序号 → 不追加 last_sequence', () => {
    const url = buildGlobalWebSocketUrl({ ticket: 't-1' } as never)
    expect(url).not.toContain('last_sequence')
  })

  it('序号为 0 → 视为无序号不追加', () => {
    expect(buildGlobalWebSocketUrl({ ticket: 't' } as never, 0)).not.toContain('last_sequence')
  })
})
