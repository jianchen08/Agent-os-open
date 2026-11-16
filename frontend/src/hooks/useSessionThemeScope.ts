/**
 * useSessionThemeScope · 主题作用域挂点（模式体系 §5.0 主题桥）
 *
 * 把当前会话的主题 override（sessionThemeStore 栈顶档）编译成 CSS 变量并
 * 应用到挂点元素子树——作用域仅聊天区 + 面板容器（挂点由消费方声明），
 * 管理面不挂点即不受影响。会话切换/出入栈自动换装与卸除（清理函数移除
 * 上一档发射的全部变量，无跨档残留）。
 *
 * useEffectiveTheme（同文件读面）：widget 层取「当前生效主题」的平台数据
 * 契约（[来源: docs/decisions/2026-10-10-widget-action-bus.md §5] widget 数据
 * 面不与具体 store 绑死）——widget 不感知 themeStore / sessionThemeStore。
 */

import { useEffect, useMemo, useState } from 'react'
import { compileThemeVariables } from '@/services/themeService'
import { useSessionStore } from '@/stores/sessionStore'
import { getActiveSessionTheme, useSessionThemeStore } from '@/stores/sessionThemeStore'
import { useThemeStore } from '@/stores/themeStore'
import type { ThemeConfig } from '@/types/theme'

/** 当前生效主题（config + 插件主题变量覆盖；override 生效时 pluginVars=null） */
export interface EffectiveTheme {
  config: ThemeConfig | null
  pluginVars: Record<string, string> | null
}

/**
 * 当前生效主题（口径与 useSessionThemeScope 同源）：活跃会话 override 栈顶
 * （面板容器作用域内实际生效者）优先；否则宿主全局主题——base 配置之上叠
 * 插件主题（contributes.themes）声明的变量覆盖。
 */
export function useEffectiveTheme(): EffectiveTheme {
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const globalThemeConfig = useThemeStore((s) => s.themeConfig)
  const activePluginTheme = useThemeStore((s) => s.activePluginTheme)
  const sessionStacks = useSessionThemeStore((s) => s.stacks)

  return useMemo(() => {
    const override = getActiveSessionTheme(activeSessionId)
    return {
      config: override ?? globalThemeConfig,
      pluginVars: override ? null : (activePluginTheme?.variables ?? null),
    }
    // sessionStacks 引用变化（入栈/出栈）即重算；getActiveSessionTheme 读栈顶
  }, [activeSessionId, sessionStacks, globalThemeConfig, activePluginTheme])
}

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
