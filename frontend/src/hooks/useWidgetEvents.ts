/**
 * useWidgetEvents — widget_event 全局订阅（单次挂载）
 *
 * 在顶层组件（如 FiveSpaceHomePage）挂载一次，订阅 WS 的 widget_event 事件，
 * 解析后派发到 widgetEventStore，供订阅了对应 widget_id 的组件渲染。
 *
 * 与 useRealtimeEvents 同模式：单挂载、effect 内订阅、cleanup 取消订阅。
 * 关联 ADR §3.5'（内核统一配置驱动推送）。
 */
import { useEffect } from 'react'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { adaptWidgetEvent } from '@/services/websocket/MessageAdapter'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import { loggers } from '@/utils/logger'

export function useWidgetEvents(): void {
  const dispatch = useWidgetEventStore((s) => s.dispatchWidgetEvent)

  useEffect(() => {
    const handleWidgetEvent = (raw: unknown) => {
      const adapted = adaptWidgetEvent(raw as Parameters<typeof adaptWidgetEvent>[0])
      if (!adapted) return
      dispatch(adapted)
    }

    globalWS.subscribe(WS_SERVER_EVENTS.WIDGET_EVENT, handleWidgetEvent)
    loggers.websocket.info('widget_event 全局订阅已注册')

    return () => {
      globalWS.unsubscribe(WS_SERVER_EVENTS.WIDGET_EVENT, handleWidgetEvent)
    }
  }, [dispatch])
}
