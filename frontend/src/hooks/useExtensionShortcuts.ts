/**
 * useExtensionShortcuts（P5-c，ADR §3.4 档位二）
 *
 * 在组件挂载时注册全局 keydown 监听：
 * - ShortcutRegistry.matchKey 查找命中命令
 * - shouldFire 检查 when 条件
 * - 命中且 when 通过 → CommandDispatcher.executeCommand
 * - 输入框聚焦时不拦截（避免编辑冲突）
 * - 卸载时移除监听
 *
 * @param shortcuts - ShortcutRegistry 实例（默认全局单例）
 * @param dispatcher - CommandDispatcher 实例（默认全局单例）
 */

import { useEffect } from 'react'
import { shortcutRegistry, ShortcutRegistry } from '@/services/schema/shortcutRegistry'
import { commandDispatcher, CommandDispatcher } from '@/services/schema/commandDispatcher'

/** 可编辑元素类型，聚焦时跳过快捷键 */
const EDITABLE_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT'])

export function useExtensionShortcuts(
  shortcuts: ShortcutRegistry = shortcutRegistry,
  dispatcher: CommandDispatcher = commandDispatcher,
): void {
  useEffect(() => {
    // 每次挂载刷新一次绑定（schema 已加载）
    shortcuts.refresh()

    const handleKeyDown = (ev: KeyboardEvent): void => {
      // 输入元素聚焦时不拦截（保留浏览器/编辑器原生行为）
      // 用 document.activeElement 判定（事件冒泡到 document 时 target 不可靠）
      const active = document.activeElement as HTMLElement | null
      if (active && (EDITABLE_TAGS.has(active.tagName) || active.isContentEditable)) {
        return
      }

      const command = shortcuts.matchKey(ev)
      if (!command) return
      if (!shortcuts.shouldFire(command)) return

      ev.preventDefault()
      void dispatcher.executeCommand(command)
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [shortcuts, dispatcher])
}
