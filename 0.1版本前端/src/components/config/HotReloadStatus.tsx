/**
 * 热加载状态指示组件
 *
 * 配置保存成功后显示「已生效（热加载）」状态，并提供「立即重载」按钮用于异常时人工触发。
 *
 * 公共接口：
 * - HotReloadStatus(props) — 热加载状态指示器
 */

import { useEffect, useRef } from 'react'
import { Loader2, CheckCircle2, RefreshCw } from 'lucide-react'
import { useConfigHotReload } from '@/hooks/useConfigHotReload'

/** HotReloadStatus 组件属性 */
export interface HotReloadStatusProps {
  /** 配置路径，用于调用 POST /api/config/reload/{path} */
  configPath: string
  /** 父级保存状态，saved 时自动展示热加载生效提示 */
  saveState: 'idle' | 'saving' | 'saved' | 'error'
}

/**
 * 热加载状态指示组件
 *
 * - saveState 变为 saved 时，自动显示「已生效（热加载）」
 * - 始终提供「立即重载」按钮，用于异常时手动触发
 * - 使用 useConfigHotReload hook 管理热加载逻辑
 */
export function HotReloadStatus({ configPath, saveState }: HotReloadStatusProps) {
  const { status, reload } = useConfigHotReload(configPath)
  const prevSaveStateRef = useRef(saveState)

  // 保存成功时自动触发热加载
  useEffect(() => {
    if (saveState === 'saved' && prevSaveStateRef.current !== 'saved') {
      reload()
    }
    prevSaveStateRef.current = saveState
  }, [saveState, reload])

  const isReloading = status === 'loading'
  const showSuccess = status === 'success' && saveState !== 'error'

  return (
    <div className="flex items-center gap-2">
      {showSuccess && (
        <span
          className="flex items-center gap-1 text-xs text-status-success"
          role="status"
        >
          <CheckCircle2 className="h-3.5 w-3.5" />
          已生效（热加载）
        </span>
      )}
      {status === 'error' && (
        <span
          className="flex items-center gap-1 text-xs text-status-error"
          role="alert"
        >
          重载失败
        </span>
      )}
      <button
        onClick={reload}
        disabled={isReloading}
        className="text-muted-foreground hover:text-foreground flex items-center gap-1 text-xs transition-colors disabled:opacity-50"
        title="手动触发配置热重载"
      >
        {isReloading ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : (
          <RefreshCw className="h-3 w-3" />
        )}
        立即重载
      </button>
    </div>
  )
}
