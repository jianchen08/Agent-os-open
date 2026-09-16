/**
 * useSessionThemeScope · 主题作用域挂点（模式体系 §5.0 主题桥）
 *
 * 把当前会话的主题 override（sessionThemeStore 栈顶档）编译成 CSS 变量并
 * 应用到挂点元素子树——作用域仅聊天区 + 面板容器（挂点由消费方声明），
 * 管理面不挂点即不受影响。会话切换/出入栈自动换装与卸除（清理函数移除
 * 上一档发射的全部变量，无跨档残留）。
 */

import { useEffect, useMemo, useState } from 'react'
import { compileThemeVariables } from '@/services/themeService'
import { useSessionStore } from '@/stores/sessionStore'
import { getActiveSessionTheme, useSessionThemeStore } from '@/stores/sessionThemeStore'
import type { ThemeConfig } from '@/types/theme'

/** compileThemeVariables 的 "a: b; c: d" 串 → 变量键值对（按首个冒号切分，值可含冒号） */
export function compileThemeVarEntries(config: ThemeConfig): Array<[string, string]> {
  return compileThemeVariables(config)
    .split(';')
    .map((entry) => entry.trim())
    .filter((entry) => entry !== '')
    .map((entry) => {
      const idx = entry.indexOf(':')
      return idx <= 0 ? null : ([entry.slice(0, idx).trim(), entry.slice(idx + 1).trim()] as [string, string])
    })
    .filter((pair): pair is [string, string] => pair !== null && pair[0].startsWith('--') && pair[1] !== '')
}

/**
 * 返回挂点 callback ref：挂到聊天区/面板容器根元素上即接入会话主题 override。
 */
export function useSessionThemeScope<T extends HTMLElement>(): (el: T | null) => void {
  const [el, setEl] = useState<T | null>(null)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const stacks = useSessionThemeStore((s) => s.stacks)

  const override = useMemo(
    () => getActiveSessionTheme(activeSessionId),
    // stacks 引用变化（入栈/出栈）即重算；getActiveSessionTheme 读栈顶
    [activeSessionId, stacks],
  )

  useEffect(() => {
    if (!el || !override) return
    const entries = compileThemeVarEntries(override)
    for (const [key, value] of entries) {
      el.style.setProperty(key, value)
    }
    return () => {
      for (const [key] of entries) {
        el.style.removeProperty(key)
      }
    }
  }, [el, override])

  return setEl
}
