/**
 * WebSocket 适配器测试 —— widget_event 族（P2 协议）
 *
 * 0.1 Python 内核消息格式适配（adaptIncomingMessage / adaptOutgoingMessage /
 * isRustKernelMessage）已随 0.1 内核退役移除，相关测试同步删除。
 */

import { describe, it, expect } from 'vitest'
import { adaptWidgetEvent } from '@/services/websocket/MessageAdapter'
import type { RawWSMessage } from '@/services/websocket/MessageAdapter'

describe('MessageAdapter — widget_event 族解析（P2 协议）', () => {
  // P2 后端将下发：{ type: 'widget_event', data: { widget_id, event, data }, sequence }
  // 数据 data 包装层是 Rust 0.2 格式，但顶层多了一个 sequence 字段需要保留。

  it('adaptWidgetEvent 解析 widget_id / event / payload / sequence', () => {
    const raw: RawWSMessage = {
      type: 'widget_event',
      data: {
        widget_id: 'review_panel',
        event: 'doc_loaded',
        data: { doc_id: 'd-1', pages: 3 },
      },
      sequence: 42,
    }

    const adapted = adaptWidgetEvent(raw)

    expect(adapted).not.toBeNull()
    expect(adapted?.widget_id).toBe('review_panel')
    expect(adapted?.event).toBe('doc_loaded')
    expect(adapted?.data).toEqual({ doc_id: 'd-1', pages: 3 })
    expect(adapted?.sequence).toBe(42)
  })

  it('adaptWidgetEvent 对非 widget_event 消息返回 null', () => {
    expect(adaptWidgetEvent({ type: 'pipeline_chunk', data: {} })).toBeNull()
    expect(adaptWidgetEvent({})).toBeNull()
  })

  it('adaptWidgetEvent 缺失 sequence 时 sequence 为 undefined', () => {
    const adapted = adaptWidgetEvent({
      type: 'widget_event',
      data: { widget_id: 'w', event: 'e', data: {} },
    })
    expect(adapted?.widget_id).toBe('w')
    expect(adapted?.sequence).toBeUndefined()
  })

  it('adaptWidgetEvent 缺失 data 包装时返回 null（防御无效格式）', () => {
    expect(
      adaptWidgetEvent({ type: 'widget_event' }),
    ).toBeNull()
  })

  it('adaptWidgetEvent 缺失 widget_id/event 时相应字段为 undefined（不抛错）', () => {
    const adapted = adaptWidgetEvent({
      type: 'widget_event',
      data: { data: { x: 1 } },
      sequence: 5,
    })
    expect(adapted?.widget_id).toBeUndefined()
    expect(adapted?.event).toBeUndefined()
    expect(adapted?.data).toEqual({ x: 1 })
    expect(adapted?.sequence).toBe(5)
  })
})
