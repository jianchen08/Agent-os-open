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

export interface WidgetEventState {
  /** widget_id → 最新一条事件（消费者只读最新值，历史队列无人读不保留） */
  latest: Record<string, AdaptedWidgetEvent>
}

export interface WidgetEventActions {
  /** 派发一条 widget 事件：按 widget_id 更新 latest。widget_id 缺失则忽略。 */
  dispatchWidgetEvent: (ev: AdaptedWidgetEvent) => void
  /** 清空（重载 schema 时调用，避免幽灵事件）。 */
  clear: () => void
}

export type WidgetEventStore = WidgetEventState & WidgetEventActions

const initialState: WidgetEventState = {
  latest: {},
}

export const useWidgetEventStore = create<WidgetEventStore>((set) => ({
  ...initialState,

  dispatchWidgetEvent: (ev) => {
    const widgetId = ev.widget_id
    if (!widgetId) return // 无 widget_id 的事件无法路由，丢弃
    set((state) => ({ latest: { ...state.latest, [widgetId]: ev } }))
  },

  clear: () => set({ ...initialState }),
}))
