/**
 * 思考模式状态管理 Hook
 *
 * 提供思考模式的状态管理和切换功能
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  checkThinkingModeSupport,
  getThinkingModeInfo,
  switchThinkingMode,
} from '@/services/api/thinkingMode'
import { reportError } from '@/services/errorReporting'
import { loggers } from '@/utils/logger'
import { uiStorage } from '@/utils/storage'
import type { ThinkingModeState, ThinkingModeType } from '@/types/thinkingMode'

export interface UseThinkingModeOptions {
  /** 当前模型名称 */
  currentModel: string
  /** 错误回调 */
  onError?: (error: string) => void
  /** 成功切换回调 */
  onSuccess?: (enabled: boolean, targetModel: string) => void
}

export interface UseThinkingModeReturn {
  /** 思考模式状态 */
  thinkingMode: ThinkingModeState
  /** 切换思考模式 */
  toggleThinkingMode: (enabled: boolean) => Promise<void>
  /** 检查模型支持 */
  checkSupport: (modelName: string) => Promise<boolean>
  /** 重置错误状态 */
  clearError: () => void
  /** 刷新模型信息 */
  refreshModelInfo: () => Promise<void>
}

// 缓存支持检查结果，避免重复请求
const supportCache = new Map<string, { result: boolean; timestamp: number }>()
const modelInfoCache = new Map<string, { info: any; timestamp: number }>()
const CACHE_DURATION = 5 * 60 * 1000 // 5分钟缓存

/**
 * 思考模式状态管理 Hook
 */
export function useThinkingMode({
  currentModel,
  onError,
  onSuccess,
}: UseThinkingModeOptions): UseThinkingModeReturn {
  /**
   * 获取初始思考模式状态
   *
   * 核心原则：显示状态是唯一真相来源
   * - localStorage 只用于持久化显示状态
   * - 页面刷新时恢复上次的显示状态
   * - 不强制验证模型支持，让后端自己处理
   */
  const getInitialEnabled = () => {
    loggers.thinkingMode.debug('===== getInitialEnabled 被调用 =====')
    loggers.thinkingMode.verbose(
      'localStorage 原始值:',
      localStorage.getItem('thinking_mode_enabled'),
    )
    loggers.thinkingMode.verbose('localStorage 解析后:', uiStorage.getThinkingModeEnabled())

    // 优先级1：从 localStorage 恢复上次的显示状态
    const stored = uiStorage.getThinkingModeEnabled()
    loggers.thinkingMode.debug('localStorage 中的值:', stored, '类型:', typeof stored)
    if (stored !== null) {
      loggers.thinkingMode.debug('使用 localStorage 的值:', stored)
      return stored
    }

    // 默认关闭
    loggers.thinkingMode.debug('localStorage 为空，使用默认值: false')
    return false
  }

  const [thinkingMode, setThinkingMode] = useState<ThinkingModeState>(() => {
    const enabled = getInitialEnabled()
    loggers.thinkingMode.debug('useState 初始化 - enabled:', enabled, 'currentModel:', currentModel)
    return {
      enabled,
      currentModel,
      switching: false,
    }
  })

  // 使用 ref 来跟踪当前正在处理的模型，避免竞态条件
  const processingModelRef = useRef<string | null>(null)
  const lastProcessedModelRef = useRef<string | null>(null)

  // 使用 ref 保存最新的 onError 回调，避免 stale closure 同时满足 exhaustive-deps 规则
  const onErrorRef = useRef(onError)
  onErrorRef.current = onError

  // 使用 ref 保存最新的 onSuccess 回调，避免 stale closure
  const onSuccessRef = useRef(onSuccess)
  onSuccessRef.current = onSuccess

  /**
   * 检查模型是否支持思考模式（带缓存）
   */
  const checkSupport = useCallback(async (modelName: string): Promise<boolean> => {
    // 防护：跳过无效的模型名
    if (!modelName || modelName === 'unknown' || modelName.trim() === '') {
      return false
    }

    // 检查缓存
    const cached = supportCache.get(modelName)
    if (cached && Date.now() - cached.timestamp < CACHE_DURATION) {
      return cached.result
    }

    try {
      const result = await checkThinkingModeSupport(modelName)
      const supports = result.supports_thinking

      // 更新缓存
      supportCache.set(modelName, { result: supports, timestamp: Date.now() })

      return supports
    } catch (error) {
      loggers.thinkingMode.error('检查思考模式支持失败:', error)
      return false
    }
  }, [])

  /**
   * 获取模型信息（带缓存和防竞态）
   */
  const fetchModelInfo = useCallback(
    async (modelName: string) => {
      loggers.thinkingMode.debug('fetchModelInfo 被调用:', modelName)
      // 防护：跳过无效的模型名
      if (!modelName || modelName === 'unknown' || modelName.trim() === '') {
        loggers.thinkingMode.debug('模型名无效，保持 enabled 状态不变')
        setThinkingMode((prev) => ({
          ...prev,
          currentModel: modelName,
          thinkingType: undefined,
          error: undefined,
        }))
        return
      }

      // 防止重复处理同一个模型
      if (processingModelRef.current === modelName) {
        return
      }

      // 如果已经处理过这个模型，跳过
      if (lastProcessedModelRef.current === modelName) {
        return
      }

      processingModelRef.current = modelName

      try {
        // 检查缓存
        const cachedInfo = modelInfoCache.get(modelName)
        if (cachedInfo && Date.now() - cachedInfo.timestamp < CACHE_DURATION) {
          loggers.thinkingMode.debug('使用缓存的模型信息')
          setThinkingMode((prev) => ({
            ...prev,
            currentModel: modelName,
            thinkingType: cachedInfo.info.thinking_type as ThinkingModeType,
            error: undefined,
          }))
          lastProcessedModelRef.current = modelName
          return
        }

        loggers.thinkingMode.debug('检查模型支持情况...')
        const isSupported = await checkSupport(modelName)
        loggers.thinkingMode.debug('模型支持情况:', isSupported)

        if (!isSupported) {
          loggers.thinkingMode.debug('模型不支持思考模式，但保持 enabled 状态')
          // 模型不支持思考模式，但不强制关闭用户的选择
          // 只是更新状态，让 UI 显示不支持
          setThinkingMode((prev) => ({
            ...prev,
            currentModel: modelName,
            thinkingType: undefined,
            error: undefined,
          }))
          lastProcessedModelRef.current = modelName
          return
        }

        loggers.thinkingMode.debug('获取模型详细信息...')
        const modelInfo = await getThinkingModeInfo(modelName)
        loggers.thinkingMode.verbose('模型信息:', modelInfo)

        // 更新缓存
        modelInfoCache.set(modelName, {
          info: modelInfo,
          timestamp: Date.now(),
        })

        setThinkingMode((prev) => ({
          ...prev,
          currentModel: modelName,
          thinkingType: modelInfo.thinking_type as ThinkingModeType,
          error: undefined,
        }))

        lastProcessedModelRef.current = modelName
      } catch (error: unknown) {
        const errorMessage =
          error instanceof Error
            ? error.message
            : typeof error === 'object' &&
                error !== null &&
                'message' in error &&
                typeof (error as any).message === 'string'
              ? (error as any).message
              : '获取模型信息失败'
        loggers.thinkingMode.error('获取思考模式信息失败:', errorMessage, error)

        setThinkingMode((prev) => ({
          ...prev,
          currentModel: modelName,
          error: errorMessage,
        }))

        // 通过 ref 调用 onError，确保始终使用最新回调且无需作为依赖项
        if (onErrorRef.current) {
          onErrorRef.current(errorMessage)
        }

        lastProcessedModelRef.current = modelName
      } finally {
        processingModelRef.current = null
      }
    },
    [checkSupport],
  )

  /**
   * 切换思考模式
   *
   * 核心原则：显示状态是唯一真相来源
   * 1. 立即更新显示状态（乐观更新）
   * 2. 保存到 localStorage
   * 3. 发送请求到后端
   * 4. 如果失败，回滚状态
   */
  const toggleThinkingMode = useCallback(
    async (enabled: boolean) => {
      // 防护：检查模型名是否有效
      if (!currentModel || currentModel === 'unknown' || currentModel.trim() === '') {
        const errorMessage = '当前模型无效，无法切换思考模式'
        loggers.thinkingMode.error('切换思考模式失败:', errorMessage)

        setThinkingMode((prev) => ({
          ...prev,
          switching: false,
          error: errorMessage,
        }))

        if (onError) {
          onError(errorMessage)
        }
        return
      }

      // 立即更新显示状态（乐观更新）
      let previousEnabled = false
      setThinkingMode((prev) => {
        previousEnabled = prev.enabled
        return {
          ...prev,
          enabled,
          switching: true,
          error: undefined,
        }
      })

      // 立即同步到 localStorage：显示状态变化时立即持久化
      uiStorage.setThinkingModeEnabled(enabled)

      try {
        const result = await switchThinkingMode(currentModel, enabled)

        // 成功：更新 thinkingType 并结束切换状态
        setThinkingMode((prev) => ({
          ...prev,
          switching: false,
          thinkingType: result.switch_type as ThinkingModeType,
          error: undefined,
        }))

        if (onSuccessRef.current) {
          onSuccessRef.current(enabled, result.target_model)
        }

        loggers.thinkingMode.info(`思考模式已${enabled ? '启用' : '关闭'}:`, {
          targetModel: result.target_model,
          switchType: result.switch_type,
          description: result.description,
        })
      } catch (error: unknown) {
        const errorMessage =
          error instanceof Error
            ? error.message
            : typeof error === 'object' &&
                error !== null &&
                'message' in error &&
                typeof (error as any).message === 'string'
              ? (error as any).message
              : '切换思考模式失败'
        loggers.thinkingMode.error('切换思考模式失败:', errorMessage, error)

        // 失败时回滚状态到之前的值
        setThinkingMode((prev) => ({
          ...prev,
          enabled: previousEnabled,
          switching: false,
          error: errorMessage,
        }))

        // 回滚 localStorage
        uiStorage.setThinkingModeEnabled(previousEnabled)

        // 报告错误
        reportError(errorMessage, 'validation', 'error', {
          code: 'THINKING_MODE_SWITCH_FAILED',
          details: { currentModel, enabled, error },
        })

        if (onErrorRef.current) {
          onErrorRef.current(errorMessage)
        }

        // 5秒后自动清除错误状态，让按钮恢复正常显示
        setTimeout(() => {
          setThinkingMode((prev) => {
            if (prev.error === errorMessage) {
              return { ...prev, error: undefined }
            }
            return prev
          })
        }, 5000)
      }
    },
    [currentModel],
  )

  /**
   * 清除错误状态
   */
  const clearError = useCallback(() => {
    setThinkingMode((prev) => ({
      ...prev,
      error: undefined,
    }))
  }, [])

  /**
   * 刷新模型信息
   */
  const refreshModelInfo = useCallback(async () => {
    // 清除缓存
    supportCache.delete(currentModel)
    modelInfoCache.delete(currentModel)
    lastProcessedModelRef.current = null

    await fetchModelInfo(currentModel)
  }, [currentModel, fetchModelInfo])

  // 当模型变化时，仅更新 currentModel 状态，不主动检测思考模式支持
  useEffect(() => {
    // 防护：只处理有效的模型名
    if (currentModel && currentModel !== 'unknown' && currentModel.trim() !== '') {
      setThinkingMode((prev) => ({
        ...prev,
        currentModel,
      }))
      lastProcessedModelRef.current = null
    } else {
      setThinkingMode((prev) => ({
        ...prev,
        currentModel,
        thinkingType: undefined,
        error: undefined,
      }))
      lastProcessedModelRef.current = null
    }
  }, [currentModel])

  return {
    thinkingMode,
    toggleThinkingMode,
    checkSupport,
    clearError,
    refreshModelInfo,
  }
}
