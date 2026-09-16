/**
 * 会话级主题 override 栈（模式体系 §5.0 主题桥 theme.apply 协议宿主侧）
 *
 * Webview 面板/消息卡经 theme.apply 上行结构化 ThemeConfig 档 → 按**收到时
 * 的当前会话**入栈（每会话独立 LIFO 栈）。生效规则：
 * - 活跃会话的栈顶档 = 当前 override；进入带主题的会话即"push 生效"，
 *   切离即"pop 回默认"（全局主题接管，会话档案保留、切回即恢复）；
 * - 载荷经 validateThemeConfig fail-closed：校验失败整包丢弃、零状态变更；
 * - 作用域仅聊天区 + 面板容器（useSessionThemeScope 消费方挂点），不动管理面。
 */

import { create } from 'zustand'
import { validateThemeConfig } from '@/services/themeService'
import type { ThemeConfig } from '@/types/theme'

export interface SessionThemeState {
  /** 每会话主题档栈（栈顶=当前生效；不入持久化——会话级视觉态不跨重启） */
  stacks: Record<string, ThemeConfig[]>
  /** 压入主题档（载荷校验失败返回 false 且零状态变更） */
  pushTheme: (sessionId: string, payload: unknown) => boolean
  /** 弹出会话栈顶档（栈空为无操作） */
  popTheme: (sessionId: string) => void
}

export const useSessionThemeStore = create<SessionThemeState>()((set, get) => ({
  stacks: {},

  pushTheme: (sessionId, payload) => {
    if (!sessionId) return false
    const { valid } = validateThemeConfig(payload)
    if (!valid) return false
    const config = payload as ThemeConfig
    set((state) => ({
      stacks: {
        ...state.stacks,
        [sessionId]: [...(state.stacks[sessionId] ?? []), config],
      },
    }))
    return true
  },

  popTheme: (sessionId) => {
    const stack = get().stacks[sessionId]
    if (!stack || stack.length === 0) return
    set((state) => ({
      stacks: {
        ...state.stacks,
        [sessionId]: state.stacks[sessionId].slice(0, -1),
      },
    }))
  },
}))

/** 当前生效的会话主题 override（栈顶；空栈/他档会话返回 undefined=默认主题） */
export function getActiveSessionTheme(sessionId: string | null | undefined): ThemeConfig | undefined {
  if (!sessionId) return undefined
  const stack = useSessionThemeStore.getState().stacks[sessionId]
  return stack && stack.length > 0 ? stack[stack.length - 1] : undefined
}
