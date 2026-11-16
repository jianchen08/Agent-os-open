/**
 * 模型上下文信息 Hook
 *
 * 根据当前使用的模型名称，从后端 LLM 配置中获取对应的 context_window 大小。
 * 匹配走三级容错：精确配置键 → 键忽略大小写 → 条目 model_name 真名
 * （管道 state 行 llm_model 是真实模型名，与配置键的大小写形态可能不一致）。
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
  /** 配置加载失败信息；null 且 isValid=false 表示配置里确实没有该模型（两种状态可区分） */
  error: string | null
  /** 刷新模型配置（手动触发） */
  refresh: () => void
}

/**
 * 模型条目三级容错匹配：精确配置键 → 键忽略大小写 → 条目 model_name 真名
 * （配置键如 minimax-m3 是小写形态，state 行 llm_model 如 MiniMax-M3 是真名；
 * 三级都未命中返回 undefined，由调用方按无效模型处理）。
 */
function matchModelConfig(
  models: Record<string, ModelConfig>,
  modelName: string,
): ModelConfig | undefined {
  const exact = models[modelName]
  if (exact) return exact
  const lower = modelName.toLowerCase()
  const key = Object.keys(models).find((k) => k.toLowerCase() === lower)
  if (key) return models[key]
  return Object.values(models).find((m) => m.model_name === modelName)
}

/**
 * 根据模型名称获取对应的 context_window
 *
 * 模型无效（空或三级匹配都未命中）时如实返回 contextWindow=0、isValid=false，
 * 由调用方决定如何显示，绝不用默认值冒充真实模型。
 *
 * @param modelName - 当前使用的模型名称
 * @returns context_window、有效性、加载状态、错误态和刷新方法
 */
export function useModelContextInfo(modelName: string | undefined): UseModelContextInfoResult {
  const [modelsCache, setModelsCache] = useState<Record<string, ModelConfig>>({})
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchModels = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const result = await getModels()
      setModelsCache(result.models)
    } catch (e) {
      // 失败进 error 态：消费方可区分「接口失败」与「配置无此模型」
      setError(e instanceof Error ? e.message : '获取模型配置失败')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchModels()
  }, [fetchModels])

  const cached = modelName ? matchModelConfig(modelsCache, modelName) : undefined
  const isValid = Boolean(cached && cached.context_window)

  return {
    contextWindow: cached?.context_window || 0,
    isValid,
    isLoading,
    error,
    refresh: fetchModels,
  }
}
