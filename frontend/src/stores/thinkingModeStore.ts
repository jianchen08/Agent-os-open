/**
 * 思考强度 Store（标签级独立记忆 + 路由联动）
 *
 * 决策（task 思考强度全链路）：
 * - 各对话标签（AgentTab）独立记忆思考强度，localStorage 持久化（key: thinking-strength-{tabId}）
 * - 切换标签（activeTabId 变化 = 路由变化）时，useActiveThinkingStrength 自动返回该标签强度，
 *   输入框随路由自动应用（"根据路由自动路由到不同思考强度"）
 * - 强度 → 模型参数映射见 STRENGTH_TO_PARAMS（随消息传给后端 llm_core 路由）
 */

import { create } from 'zustand'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { DEFAULT_THINKING_STRENGTH, type ThinkingStrength } from '@/types/thinkingMode'

/** localStorage 键前缀（按标签独立记忆） */
const STORAGE_KEY_PREFIX = 'thinking-strength-'

function getStorageKey(tabId: string): string {
  return `${STORAGE_KEY_PREFIX}${tabId}`
}

/** 从 localStorage 惰性读强度，非法/缺失回退默认 */
function loadStrength(tabId: string): ThinkingStrength {
  try {
    const raw = localStorage.getItem(getStorageKey(tabId))
    if (raw === 'off' || raw === 'low' || raw === 'medium' || raw === 'high') return raw
  } catch {
    // storage 不可用（隐私模式等）→ 默认
  }
  return DEFAULT_THINKING_STRENGTH
}

interface ThinkingModeState {
  /** tabId → 思考强度（内存态，未设置的标签不占位） */
  strengthByTabId: Record<string, ThinkingStrength>
  /** 读取某标签强度（缺失回退默认，惰性读 localStorage） */
  getStrength: (tabId: string) => ThinkingStrength
  /** 读取某标签的显式设置强度；未设置过（用户未选过）返回 null。
   *   null 时调用方可用管道模型参数反向映射初始档（mapParamsToStrength）。 */
  getExplicitStrength: (tabId: string) => ThinkingStrength | null
  /** 设置某标签强度（写内存 + 持久化） */
  setStrength: (tabId: string, strength: ThinkingStrength) => void
}

export const useThinkingModeStore = create<ThinkingModeState>((set, get) => ({
  strengthByTabId: {},

  getStrength: (tabId) => {
    const inMemory = get().strengthByTabId[tabId]
    if (inMemory) return inMemory
    // 惰性读 localStorage（首次访问该标签时恢复）
    const stored = loadStrength(tabId)
    if (stored !== DEFAULT_THINKING_STRENGTH) {
      set((state) => ({ strengthByTabId: { ...state.strengthByTabId, [tabId]: stored } }))
    }
    return stored
  },

  getExplicitStrength: (tabId) => {
    const inMemory = get().strengthByTabId[tabId]
    if (inMemory) return inMemory
    try {
      const raw = localStorage.getItem(getStorageKey(tabId))
      if (raw === 'off' || raw === 'low' || raw === 'medium' || raw === 'high') return raw
    } catch {
      // storage 不可用 → null
    }
    return null
  },

  setStrength: (tabId, strength) => {
    try {
      localStorage.setItem(getStorageKey(tabId), strength)
    } catch {
      // storage 不可用 → 仅内存态
    }
    set((state) => ({ strengthByTabId: { ...state.strengthByTabId, [tabId]: strength } }))
  },
}))

/**
 * 当前激活标签的思考强度（路由联动）：
 * 订阅 agentTabStore.activeTabId → 切标签自动应用该标签保存的强度；无标签时默认。
 */
export function useActiveThinkingStrength(): ThinkingStrength {
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  return useThinkingModeStore((s) => s.getStrength(activeTabId ?? ''))
}

/**
 * 当前激活标签的显式思考强度（未设置过返回 null）。
 * 供 ChatContainer 组合管道参数映射：explicit ?? mapParamsToStrength(pipelineParams) ?? 默认。
 */
export function useExplicitThinkingStrength(): ThinkingStrength | null {
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  return useThinkingModeStore((s) => (activeTabId ? s.getExplicitStrength(activeTabId) : null))
}
