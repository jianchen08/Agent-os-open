/**
 * 配置热加载 Hook
 *
 * 封装调用后端 POST /api/v1/config/reload/{path} 的逻辑，
 * 管理 loading / success / error 三种状态。
 *
 * 公共接口：
 * - useConfigHotReload(configPath) — 返回 { status, reload, reset }
 */

import { useState, useCallback } from 'react'
import { reloadConfig } from '@/services/api/config'

/** 热加载状态 */
export type HotReloadStatus = 'idle' | 'loading' | 'success' | 'error'

/** Hook 返回值 */
export interface UseConfigHotReloadResult {
  /** 当前热加载状态 */
  status: HotReloadStatus
  /** 手动触发配置重载 */
  reload: () => Promise<void>
  /** 重置状态为 idle */
  reset: () => void
}

/**
 * 配置热加载 Hook
 *
 * @param configPath 配置路径（与后端热加载注册的路径一致，如 "llm/defaults"）
 * @returns 热加载状态和操作函数
 *
 * @example
 * ```tsx
 * const { status, reload } = useConfigHotReload('llm/defaults')
 *
 * // 保存成功后自动触发
 * const handleSave = async () => {
 *   await saveConfig(data)
 *   await reload()
 * }
 * ```
 */
export function useConfigHotReload(configPath: string): UseConfigHotReloadResult {
  const [status, setStatus] = useState<HotReloadStatus>('idle')

  const reload = useCallback(async () => {
    setStatus('loading')
    try {
      await reloadConfig(configPath)
      setStatus('success')
    } catch {
      setStatus('error')
    }
  }, [configPath])

  const reset = useCallback(() => {
    setStatus('idle')
  }, [])

  return { status, reload, reset }
}
