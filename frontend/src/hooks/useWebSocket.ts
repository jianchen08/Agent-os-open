/**
 * WebSocket 连接管理 Hook
 *
 * 暴露接口：
 * - useWebSocket(): WebSocket 连接管理和事件订阅
 */

import { useEffect, useState, useCallback, useRef } from 'react'
import {
  webSocketService,
  WebSocketStatus,
  type WebSocketStatusType,
} from '@/services/websocket/WebSocketService'
import type { EventHandler } from '@/services/websocket/eventHandlers'

export interface UseWebSocketReturn {
  /** 连接状态 */
  status: WebSocketStatusType
  /** 是否已连接 */
  connected: boolean
  /** 是否正在连接 */
  connecting: boolean
  /** 是否正在重连 */
  reconnecting: boolean
  /** 是否连接失败 */
  failed: boolean
  /** 订阅事件 */
  subscribe: (event: string, handler: EventHandler) => () => void
  /** 取消订阅 */
  unsubscribe: (event: string, handler: EventHandler) => void
  /** 手动重连 */
  reconnect: () => void
}

interface Subscription {
  event: string
  handler: EventHandler
}

/**
 * WebSocket 连接管理 Hook
 */
export function useWebSocket(): UseWebSocketReturn {
  const [status, setStatus] = useState<WebSocketStatusType>(WebSocketStatus.DISCONNECTED)
  const subscriptionsRef = useRef<Set<Subscription>>(new Set())

  // 更新连接状态
  useEffect(() => {
    const handleStatusChange = () => {
      setStatus(webSocketService.getStatus())
    }

    // 订阅内部连接状态变化事件
    webSocketService.subscribe('connect', handleStatusChange)
    webSocketService.subscribe('disconnect', handleStatusChange)
    webSocketService.subscribe('error', handleStatusChange)

    // 初始化状态
    handleStatusChange()

    return () => {
      webSocketService.unsubscribe('connect', handleStatusChange)
      webSocketService.unsubscribe('disconnect', handleStatusChange)
      webSocketService.unsubscribe('error', handleStatusChange)
    }
  }, [])

  /**
   * 订阅事件
   */
  const subscribe = useCallback((event: string, handler: EventHandler) => {
    const subscription: Subscription = { event, handler }
    subscriptionsRef.current.add(subscription)
    webSocketService.subscribe(event, handler)

    // 返回取消订阅函数
    return () => {
      subscriptionsRef.current.delete(subscription)
      webSocketService.unsubscribe(event, handler)
    }
  }, [])

  /**
   * 取消订阅
   */
  const unsubscribe = useCallback((event: string, handler: EventHandler) => {
    webSocketService.unsubscribe(event, handler)
    // 从订阅集合中移除
    subscriptionsRef.current.forEach((sub) => {
      if (sub.event === event && sub.handler === handler) {
        subscriptionsRef.current.delete(sub)
      }
    })
  }, [])

  /**
   * 手动重连
   */
  const reconnect = useCallback(() => {
    const threadId = webSocketService.getThreadId()
    const token = webSocketService.getToken()

    if (threadId && token) {
      webSocketService.connect(threadId, token)
    } else if (token) {
      webSocketService.connect(token)
    } else {
      console.warn('[useWebSocket] 无法重连：缺少连接参数')
    }
  }, [])

  // 组件卸载时取消所有订阅
  useEffect(() => {
    return () => {
      subscriptionsRef.current.forEach(({ event, handler }) => {
        webSocketService.unsubscribe(event, handler)
      })
      subscriptionsRef.current.clear()
    }
  }, [])

  return {
    status,
    connected: status === WebSocketStatus.CONNECTED,
    connecting: status === WebSocketStatus.CONNECTING,
    reconnecting: status === WebSocketStatus.RECONNECTING,
    failed: status === WebSocketStatus.FAILED,
    subscribe,
    unsubscribe,
    reconnect,
  }
}
