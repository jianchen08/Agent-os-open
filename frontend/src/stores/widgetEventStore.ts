/**
 * WidgetEventStore — 插件 widget 事件总线（前端侧消费）
 *
 * 接收内核 widget_event 推送（PluginWidgetBroadcaster 周期快照 + 插件一次性事件），
 * 按 widget_id 分发，供订阅了该 widget 的组件渲染。
 *
 * 与 useWidgetEvents hook 配合：hook 负责订阅 WS 的 widget_event 事件并调用
 * dispatchWidgetEvent；本 store 负责存储与派发。
 *
 * 不持久化（transient，刷新即失）。
 *
 * 关联 ADR §3.5'（内核统一配置驱动推送）。
 */
import { create } from 'zustand'
import type { AdaptedWidgetEvent } from '@/services/websocket/MessageAdapter'

/** 每个 widget_id 最多保留的历史事件条数（防内存膨胀）。 */
const MAX_EVENTS_PER_WIDGET = 50

export interface WidgetEventState {
  /** widget_id → 事件队列（最新在末尾，超过上限裁掉最早的） */
  events: Record<string, AdaptedWidgetEvent[]>
  /** widget_id → 最新一条事件（高频 widget 订阅 latest 即可，无需遍历队列） */
  latest: Record<string, AdaptedWidgetEvent>
}

export interface WidgetEventActions {
  /** 派发一条 widget 事件：按 widget_id 追加队列 + 更新 latest。widget_id 缺失则忽略。 */
  dispatchWidgetEvent: (ev: AdaptedWidgetEvent) => void
  /** 清空（重载 schema 时调用，避免幽灵事件）。 */
  clear: () => void
}

export type WidgetEventStore = WidgetEventState & WidgetEventActions

const initialState: WidgetEventState = {
  events: {},
  latest: {},
}

export const useWidgetEventStore = create<WidgetEventStore>((set) => ({
  ...initialState,

  dispatchWidgetEvent: (ev) => {
    const widgetId = ev.widget_id
    if (!widgetId) return // 无 widget_id 的事件无法路由，丢弃
    set((state) => {
      const prevQueue = state.events[widgetId] ?? []
      const nextQueue =
        prevQueue.length >= MAX_EVENTS_PER_WIDGET
          ? [...prevQueue.slice(prevQueue.length - MAX_EVENTS_PER_WIDGET + 1), ev]
          : [...prevQueue, ev]
      return {
        events: { ...state.events, [widgetId]: nextQueue },
        latest: { ...state.latest, [widgetId]: ev },
      }
    })
  },

  clear: () => set({ ...initialState }),
}))
