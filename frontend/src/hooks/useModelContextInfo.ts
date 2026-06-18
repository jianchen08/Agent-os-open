/**
 * 模型上下文信息 Hook
 *
 * 根据当前使用的模型名称，从后端 LLM 配置中获取对应的 context_window 大小。
 */

import { useCallback, useEffect, useState } from 'react'
import { getModels, type ModelConfig } from '@/services/api/config'

interface UseModelContextInfoResult {
  /** 当前模型的 context_window 大小（token 数） */
  contextWindow: number
  /** 是否正在加载模型配置 */
  isLoading: boolean
  /** 刷新模型配置（手动触发） */
  refresh: () => void
}

const DEFAULT_CONTEXT_WINDOW = 128000

/**
 * 根据模型名称获取对应的 context_window
 *
 * @param modelName - 当前使用的模型名称
 * @returns context_window、加载状态和刷新方法
 */
export function useModelContextInfo(modelName: string | undefined): UseModelContextInfoResult {
  const [modelsCache, setModelsCache] = useState<Record<string, ModelConfig>>({})
  const [isLoading, setIsLoading] = useState(false)

  const fetchModels = useCallback(async () => {
    setIsLoading(true)
    try {
      const result = await getModels()
      setModelsCache(result.models)
    } catch (error) {
      console.error('获取模型配置失败:', error)
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchModels()
  }, [fetchModels])

  const contextWindow = (() => {
    if (!modelName || !modelsCache[modelName]) {
      return DEFAULT_CONTEXT_WINDOW
    }
    return modelsCache[modelName].context_window || DEFAULT_CONTEXT_WINDOW
  })()

  return {
    contextWindow,
    isLoading,
    refresh: fetchModels,
  }
}
