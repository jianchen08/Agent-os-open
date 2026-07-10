/**
 * 消息 Token 计数 Hook
 *
 * 使用后端 API 计算消息列表的 Token 数量
 * 支持防抖优化
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { countMessagesTokens } from '@/services/api/tokens'
import type { Message } from '@/types'

interface UseMessageTokensOptions {
  /** 防抖延迟（毫秒），默认 300ms */
  debounceMs?: number
  /** 是否启用，默认 true */
  enabled?: boolean
  /** 模型名称，用于选择编码器 */
  model?: string
}

/**
 * 消息 Token 计数 Hook
 *
 * @param options - 配置选项
 * @returns Token 数量和加载状态
 */
export function useMessageTokens(options: UseMessageTokensOptions = {}) {
  const { debounceMs = 300, enabled = true, model = 'gpt-4' } = options

  const [tokenCount, setTokenCount] = useState(0)
  const [isLoading, setIsLoading] = useState(false)
  const timeoutRef = useRef<NodeJS.Timeout | null>(null)
  const abortControllerRef = useRef<AbortController | null>(null)

  /**
   * 计算消息列表的 Token 数量（带防抖）
   */
  const calculateTokens = useCallback(
    (messages: Message[]) => {
      if (!enabled) {
        setTokenCount(0)
        return
      }

      // 取消之前的请求
      if (abortControllerRef.current) {
        abortControllerRef.current.abort()
      }

      // 清除之前的定时器
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }

      // 空消息列表直接返回 0
      if (!messages || messages.length === 0) {
        setTokenCount(0)
        return
      }

      // 设置新的定时器
      timeoutRef.current = setTimeout(async () => {
        try {
          setIsLoading(true)
          abortControllerRef.current = new AbortController()

          // 转换消息格式
          const messagesPayload = messages.map((msg) => ({
            role: msg.role,
            content: typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content),
          }))

          const result = await countMessagesTokens({
            messages: messagesPayload,
            model,
          })

          setTokenCount(result.token_count)
        } catch (error) {
          // 如果是主动取消的请求，不处理错误
          if (error instanceof Error && error.name === 'AbortError') {
            return
          }
          console.error('消息 Token 计算失败:', error)

          // 失败时使用估算方案：每条消息平均 100 tokens
          const fallback = messages.length * 100
          setTokenCount(fallback)
        } finally {
          setIsLoading(false)
        }
      }, debounceMs)
    },
    [debounceMs, enabled, model],
  )

  // 清理定时器和请求
  useEffect(() => {
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }
      if (abortControllerRef.current) {
        abortControllerRef.current.abort()
      }
    }
  }, [])

  return {
    tokenCount,
    isLoading,
    calculateTokens,
  }
}
