/**
 * Token 计数 Hook
 *
 * 使用后端 API 精确计算文本 Token 数量
 * 包含防抖优化，避免频繁请求
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { countTokens } from '@/services/api'

interface UseTokenCountOptions {
  /** 防抖延迟（毫秒），默认 500ms */
  debounceMs?: number
  /** 是否启用，默认 true */
  enabled?: boolean
}

/**
 * Token 计数 Hook
 *
 * @param options - 配置选项
 * @returns Token 数量和加载状态
 */
export function useTokenCount(options: UseTokenCountOptions = {}) {
  const { debounceMs = 500, enabled = true } = options

  const [tokenCount, setTokenCount] = useState(0)
  const [isLoading, setIsLoading] = useState(false)
  const timeoutRef = useRef<NodeJS.Timeout | null>(null)

  /**
   * 计算文本的 Token 数量（带防抖）
   */
  const countTokensDebounced = useCallback(
    (text: string) => {
      if (!enabled) {
        setTokenCount(0)
        return
      }

      // 清除之前的定时器
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }

      // 空文本直接返回 0
      if (!text || text.trim().length === 0) {
        setTokenCount(0)
        return
      }

      // 设置新的定时器
      timeoutRef.current = setTimeout(async () => {
        try {
          setIsLoading(true)
          const result = await countTokens({ text, model: 'gpt-4' })
          setTokenCount(result.token_count)
        } catch (error) {
          console.error('Token 计算失败:', error)
          // 失败时使用字符数作为降级方案
          setTokenCount(Math.ceil(text.length / 2))
        } finally {
          setIsLoading(false)
        }
      }, debounceMs)
    },
    [debounceMs, enabled],
  )

  /**
   * 直接计算（不带防抖，用于某些需要立即计算的场景）
   */
  const countTokensImmediate = useCallback(
    async (text: string) => {
      if (!enabled || !text || text.trim().length === 0) {
        setTokenCount(0)
        return 0
      }

      try {
        setIsLoading(true)
        const result = await countTokens({ text, model: 'gpt-4' })
        setTokenCount(result.token_count)
        return result.token_count
      } catch (error) {
        console.error('Token 计算失败:', error)
        // 失败时使用字符数作为降级方案
        const fallback = Math.ceil(text.length / 2)
        setTokenCount(fallback)
        return fallback
      } finally {
        setIsLoading(false)
      }
    },
    [enabled],
  )

  // 清理定时器
  useEffect(() => {
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current)
      }
    }
  }, [])

  return {
    tokenCount,
    isLoading,
    countTokens: countTokensDebounced,
    countTokensImmediate,
  }
}
