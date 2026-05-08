/**
 * WebSocket 连接管理 Hook
 *
 * 暴露接口：
 * - useWebSocket(): WebSocket 连接管理和事件订阅
 *
 * 已切换到连接池模式：所有事件通过 wsPool 订阅，
 * 支持多会话并行，每个管道独立流式输出。
 */

import { useEffect, useState, useCallback, useRef } from 'react'
import { wsPool } from '@/services/websocket/WebSocketConnectionPool'
import { WebSocketStatus, type WebSocketStatusType } from '@/services/websocket/WebSocketService'
import type { EventHandler } from '@/services/websocket/eventHandlers'

export interface UseWebSocketReturn {
  status: WebSocketStatusType
  connected: boolean
  connecting: boolean
  reconnecting: boolean
  failed: boolean
  subscribe: (event: string, handler: EventHandler) => () => void
  unsubscribe: (event: string, handler: EventHandler) => void
  reconnect: () => void
}

interface Subscription {
  event: string
  handler: EventHandler
}

/**
 * WebSocket 连接管理 Hook
 *
 * 通过连接池订阅事件，支持多会话并行。
 */
export function useWebSocket(): UseWebSocketReturn {
  const [status, setStatus] = useState<WebSocketStatusType>(wsPool.getStatus())
  const subscriptionsRef = useRef<Set<Subscription>>(new Set())

  useEffect(() => {
    const poll = setInterval(() => {
      setStatus(wsPool.getStatus())
    }, 2000)

    setStatus(wsPool.getStatus())
    return () => clearInterval(poll)
  }, [])

  const subscribe = useCallback((event: string, handler: EventHandler) => {
    const subscription: Subscription = { event, handler }
    subscriptionsRef.current.add(subscription)
    wsPool.subscribe(event, handler)

    return () => {
      subscriptionsRef.current.delete(subscription)
      wsPool.unsubscribe(event, handler)
    }
  }, [])

  const unsubscribe = useCallback((event: string, handler: EventHandler) => {
    wsPool.unsubscribe(event, handler)
    subscriptionsRef.current.forEach((sub) => {
      if (sub.event === event && sub.handler === handler) {
        subscriptionsRef.current.delete(sub)
      }
    })
  }, [])

  const reconnect = useCallback(() => {
    const activeThread = wsPool.getActiveThread()
    if (activeThread) {
      const conn = wsPool.getConnection(activeThread)
      if (conn) {
        const token = conn.getToken()
        if (token) {
          wsPool.disconnect(activeThread)
          wsPool.connect(activeThread, token)
        }
      }
    }
  }, [])

  useEffect(() => {
    return () => {
      subscriptionsRef.current.forEach(({ event, handler }) => {
        wsPool.unsubscribe(event, handler)
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
