/**
 * useWidgetEventValue — widget_event 推送通道的组件侧读面
 *
 * [来源: docs/decisions/2026-10-10-widget-action-bus.md §5] widget 数据面不与
 * 具体 store 绑死：widget_event 推送（metric_bindings 配置驱动，内核 →
 * widgetEventStore → 组件渲染的「最后一公里」）经本 hook 暴露给 widget 层，
 * widget 只见「按 widgetId 取最新推送事件」的平台契约，不感知存储实现。
 *
 * 与 useWidgetEvents（写面）配对：hook 订阅 WS widget_event 事件并派发进
 * store；本 hook 是组件读面。与 datasourceUri（HTTP 拉取声明面）互补——
 * 推送通道无 HTTP 轮询形态，不走 datasource 代理。
 */

import { useWidgetEventStore } from '@/stores/widgetEventStore'
import type { AdaptedWidgetEvent } from '@/services/websocket/MessageAdapter'

/**
 * 读取指定 widget 实例的最新推送事件（无推送/未传 widgetId 返回 undefined）。
 *
 * @param widgetId - widget 实例 id（插件声明 widgetId；未传不订阅任何实例）
 */
export function useWidgetEventValue(widgetId?: string): AdaptedWidgetEvent | undefined {
  return useWidgetEventStore((s) => (widgetId ? s.latest[widgetId] : undefined))
}
