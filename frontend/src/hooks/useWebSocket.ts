/**
 * WebSocket 连接管理 Hook
 *
 * 基于 GlobalWebSocket 单连接模式。
 * 已从 WebSocketConnectionPool 迁移。
 */

import { useEffect, useState, useCallback, useRef } from 'react'
import { globalWS, type ConnectionStatus } from '@/services/websocket/GlobalWebSocket'

export interface UseWebSocketReturn {
  status: ConnectionStatus
  connected: boolean
  connecting: boolean
  subscribe: (event: string, handler: (data: any) => void) => () => void
  unsubscribe: (event: string, handler: (data: any) => void) => void
  reconnect: () => void
}

interface Subscription {
  event: string
  handler: (data: any) => void
}

export function useWebSocket(): UseWebSocketReturn {
  const [status, setStatus] = useState<ConnectionStatus>(globalWS.status)
  const subscriptionsRef = useRef<Set<Subscription>>(new Set())

  useEffect(() => {
    const handleStatus = (data: { status: string }) => {
      setStatus(data.status as ConnectionStatus)
    }
    globalWS.subscribe('_status', handleStatus)
    setStatus(globalWS.status)

    return () => {
      globalWS.unsubscribe('_status', handleStatus)
    }
  }, [])

  const subscribe = useCallback((event: string, handler: (data: any) => void) => {
    const subscription: Subscription = { event, handler }
    subscriptionsRef.current.add(subscription)
    globalWS.subscribe(event, handler)

    return () => {
      subscriptionsRef.current.delete(subscription)
      globalWS.unsubscribe(event, handler)
    }
  }, [])

  const unsubscribe = useCallback((event: string, handler: (data: any) => void) => {
    globalWS.unsubscribe(event, handler)
    subscriptionsRef.current.forEach((sub) => {
      if (sub.event === event && sub.handler === handler) {
        subscriptionsRef.current.delete(sub)
      }
    })
  }, [])

  const reconnect = useCallback(() => {
    if (globalWS.status === 'disconnected') {
      console.info('[useWebSocket] reconnect 不再需要：GlobalWebSocket 自动重连')
    }
  }, [])

  useEffect(() => {
    return () => {
      subscriptionsRef.current.forEach(({ event, handler }) => {
        globalWS.unsubscribe(event, handler)
      })
      subscriptionsRef.current.clear()
    }
  }, [])

  return {
    status,
    connected: status === 'connected',
    connecting: status === 'connecting',
    subscribe,
    unsubscribe,
    reconnect,
  }
}
