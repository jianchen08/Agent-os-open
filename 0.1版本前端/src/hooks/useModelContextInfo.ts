/**
 * 模型上下文信息 Hook
 *
 * 根据当前使用的模型名称，从后端 LLM 配置中获取对应的 context_window 大小。
 */

import { useCallback, useEffect, useState } from 'react'
import { getModels, type ModelConfig } from '@/services/api/config'

interface UseModelContextInfoResult {
  /** 当前模型的 context_window 大小（token 数），模型无效时为 0 */
  contextWindow: number
  /** 当前模型是否有效（拿到真实 context_window） */
  isValid: boolean
  /** 是否正在加载模型配置 */
  isLoading: boolean
  /** 刷新模型配置（手动触发） */
  refresh: () => void
}

/**
 * 根据模型名称获取对应的 context_window
 *
 * 模型无效（空或未配置）时如实返回 contextWindow=0、isValid=false，
 * 由调用方决定如何显示，绝不用默认值冒充真实模型。
 *
 * @param modelName - 当前使用的模型名称
 * @returns context_window、有效性、加载状态和刷新方法
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

  const cached = modelName ? modelsCache[modelName] : undefined
  const isValid = Boolean(cached && cached.context_window)

  return {
    contextWindow: cached?.context_window || 0,
    isValid,
    isLoading,
    refresh: fetchModels,
  }
}
