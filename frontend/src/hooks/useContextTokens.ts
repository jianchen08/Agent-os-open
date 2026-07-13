/**
 * 上下文 Token 计数 Hook
 *
 * 使用后端 API 获取上下文 Token 使用量，包括系统提示、工具定义、历史消息等所有上下文 Token 计数。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { getContextTokenUsage } from '@/services/api/sessions'

interface UseContextTokensOptions {
  /** 防抖延迟（毫秒），默认 500ms */
  debounceMs?: number
  /** 是否启用，默认 true */
  enabled?: boolean
  /** 父执行记录 ID，用于获取精确的上下文 token 使用量 */
  parentExecutionRecordId?: string
}

/**
 * 上下文 Token 计数 Hook
 *
 * @param options - 配置选项
 * @returns Token 数量、模型名称、上下文窗口、加载状态和是否为估算值
 */
export function useContextTokens(options: UseContextTokensOptions = {}) {
  const { debounceMs = 500, enabled = true } = options

  const [tokenCount, setTokenCount] = useState(0)
  const [modelName, setModelName] = useState<string>('unknown')
  const [contextWindow, setContextWindow] = useState<number>(128000)
  const [isLoading, setIsLoading] = useState(false)
  const [isEstimated, setIsEstimated] = useState(false)
  const timeoutRef = useRef<NodeJS.Timeout | null>(null)
  const abortControllerRef = useRef<AbortController | null>(null)
  const lastThreadRef = useRef<string | null>(null)
  const lastParentExecutionRecordIdRef = useRef<string | null>(null)

  /**
   * 获取线程的上下文 Token 统计（带防抖）
   */
  const fetchContextTokens = useCallback(
    (threadId: string | null | undefined, parentExecutionRecordId?: string) => {
      if (!enabled || !threadId) {
        setTokenCount(0)
        setModelName('unknown')
        setContextWindow(128000)
        setIsEstimated(false)
        return
      }

      // 跳过临时线程，避免 404 错误
      // 临时线程只存在于前端状态，未保存到数据库
      if (threadId.startsWith('temp-')) {
        setTokenCount(0)
        setModelName('unknown')
        setContextWindow(128000)
        setIsEstimated(false)
        return
      }

      // 避免重复请求同一个线程和父执行记录ID组合
      const currentParentId = parentExecutionRecordId || ''
      if (
        lastThreadRef.current === threadId &&
        lastParentExecutionRecordIdRef.current === currentParentId &&
        !isLoading
      ) {
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

      // 设置新的定时器
      timeoutRef.current = setTimeout(async () => {
        try {
          setIsLoading(true)
          abortControllerRef.current = new AbortController()

          let result
          let estimated = false

          // 当提供了 parentExecutionRecordId 时，调用新API获取精确的上下文token
          if (parentExecutionRecordId) {
            result = await getContextTokenUsage(threadId, parentExecutionRecordId)
            estimated = result.is_estimated
          } else {
            // 没有提供 parentExecutionRecordId 时，调用估算API
            result = await getContextTokenUsage(threadId)
            estimated = true
          }

          // 后端返回 current_context_tokens，映射到 tokenCount
          setTokenCount(result.current_context_tokens || result.total_tokens || 0)
          setModelName(result.model || result.model_name || 'unknown')
          setContextWindow(result.context_window || 128000)
          setIsEstimated(estimated)

          lastThreadRef.current = threadId
          lastParentExecutionRecordIdRef.current = currentParentId
        } catch (error) {
          // 如果是主动取消的请求，不处理错误
          if (error instanceof Error && error.name === 'AbortError') {
            return
          }
          console.error('获取上下文 Token 统计失败:', error)

          // 失败时保持之前的状态，不重置
          // 这样用户可以看到最后成功的统计
        } finally {
          setIsLoading(false)
        }
      }, debounceMs)
    },
    [debounceMs, enabled],
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
    modelName,
    contextWindow,
    isLoading,
    isEstimated,
    fetchContextTokens,
  }
}
