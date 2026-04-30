/**
 * 键盘快捷键 Hook
 *
 * 管理会话页面的键盘快捷键
 * - Ctrl+G: 切换执行图面板
 * - Ctrl+T: 切换任务状态面板
 * - Escape: 关闭所有面板
 */

import { useEffect } from 'react'
import { useUIStore } from '@/stores/uiStore'

/**
 * 键盘快捷键配置
 */
interface ShortcutConfig {
  /** 快捷键描述 */
  description: string
  /** 处理函数 */
  handler: () => void
  /** 是否启用 */
  enabled?: boolean
}

/**
 * 使用键盘快捷键
 */
export const useKeyboardShortcuts = (shortcuts: Record<string, ShortcutConfig>) => {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      // 检查是否在输入框中
      const target = event.target as HTMLElement
      const isInInput =
        target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.contentEditable === 'true'

      // 如果在输入框中，不处理快捷键（Escape除外）
      if (isInInput && event.key !== 'Escape') {
        return
      }

      // 构建快捷键标识
      const key = event.key.toLowerCase()
      const modifiers: string[] = []
      if (event.ctrlKey) modifiers.push('ctrl')
      if (event.altKey) modifiers.push('alt')
      if (event.shiftKey) modifiers.push('shift')
      if (event.metaKey) modifiers.push('meta')

      const shortcutId = [...modifiers, key].join('+')

      // 查找匹配的快捷键
      const shortcut = shortcuts[shortcutId]
      if (shortcut && shortcut?.enabled !== false && shortcut.handler) {
        event.preventDefault()
        shortcut.handler()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [shortcuts])
}

/**
 * 会话页面快捷键 Hook
 *
 * 预配置的会话页面快捷键
 */
export const useSessionShortcuts = () => {
  const {
    toggleExecutionGraph,
    toggleTaskPanel,
    setExecutionGraphCollapsed,
    setTaskPanelCollapsed,
  } = useUIStore()

  useKeyboardShortcuts({
    'ctrl+g': {
      description: '切换执行图面板',
      handler: toggleExecutionGraph,
    },
    'ctrl+t': {
      description: '切换任务状态面板',
      handler: toggleTaskPanel,
    },
    escape: {
      description: '关闭所有面板',
      handler: () => {
        setExecutionGraphCollapsed(true)
        setTaskPanelCollapsed(true)
      },
    },
  })
}
