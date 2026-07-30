/**
 * widgetEventStore 单测（ADR §3.5' 内核统一配置驱动推送的前端消费层）
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import type { AdaptedWidgetEvent } from '@/services/websocket/MessageAdapter'

function ev(widgetId: string, value: number, sequence?: number): AdaptedWidgetEvent {
  return { widget_id: widgetId, event: 'snapshot', data: { value }, sequence }
}

describe('widgetEventStore', () => {
  beforeEach(() => {
    useWidgetEventStore.getState().clear()
  })

  it('dispatchWidgetEvent 按 widget_id 追加队列 + 更新 latest', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent(ev('w1', 1, 1))
    dispatchWidgetEvent(ev('w1', 2, 2))
    dispatchWidgetEvent(ev('w2', 99, 3))

    const state = useWidgetEventStore.getState()
    expect(state.events['w1']).toHaveLength(2)
    expect(state.latest['w1'].data.value).toBe(2)
    expect(state.latest['w2'].data.value).toBe(99)
  })

  it('无 widget_id 的事件被忽略', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent({ event: 'orphan', data: {} })
    const state = useWidgetEventStore.getState()
    expect(Object.keys(state.events)).toHaveLength(0)
    expect(Object.keys(state.latest)).toHaveLength(0)
  })

  it('队列超过上限裁掉最早事件（保留最新 N 条）', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    // 默认上限 50，发 60 条
    for (let i = 1; i <= 60; i++) {
      dispatchWidgetEvent(ev('w1', i, i))
    }
    const queue = useWidgetEventStore.getState().events['w1']
    expect(queue).toHaveLength(50)
    // 最早应是第 11 条（裁掉 1-10），最新是第 60 条
    expect(queue[0].data.value).toBe(11)
    expect(queue[49].data.value).toBe(60)
    expect(useWidgetEventStore.getState().latest['w1'].data.value).toBe(60)
  })

  it('clear 清空所有', () => {
    const { dispatchWidgetEvent, clear } = useWidgetEventStore.getState()
    dispatchWidgetEvent(ev('w1', 1, 1))
    clear()
    const state = useWidgetEventStore.getState()
    expect(state.events).toEqual({})
    expect(state.latest).toEqual({})
  })

  it('不同 widget_id 队列互不干扰', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent(ev('w1', 1, 1))
    dispatchWidgetEvent(ev('w2', 2, 2))
    dispatchWidgetEvent(ev('w1', 3, 3))
    const state = useWidgetEventStore.getState()
    expect(state.events['w1']).toHaveLength(2)
    expect(state.events['w2']).toHaveLength(1)
  })
})
