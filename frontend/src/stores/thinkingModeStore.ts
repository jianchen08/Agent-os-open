/**
 * 思考选择 Store（标签级独立记忆 + 路由联动）
 *
 * - 各对话标签（AgentTab）独立记忆思考选择，localStorage 持久化
 *   （key: thinking-strength-{tabId}）；切换标签时选择随路由自动应用
 * - 选择值 = thinking_strength_params 配置的参数组 JSON 串（选项由
 *   llm_service thinking-levels 端点下发，选中即随消息透传）；本侧只负责
 *   选择与记忆，零思考业务知识
 */

import { create } from 'zustand'
import { useAgentTabStore } from '@/stores/agentTabStore'
import type { ThinkingStrength } from '@/types/thinkingMode'

/** localStorage 键前缀（按标签独立记忆） */
const STORAGE_KEY_PREFIX = 'thinking-strength-'

function getStorageKey(tabId: string): string {
  return `${STORAGE_KEY_PREFIX}${tabId}`
}

/** 从 localStorage 惰性读选择，缺失回退 ''（= 未选择，消息不带思考参数） */
function loadStrength(tabId: string): ThinkingStrength {
  try {
    return localStorage.getItem(getStorageKey(tabId)) ?? ''
  } catch {
    // storage 不可用（隐私模式等）→ 未选择
    return ''
  }
}

interface ThinkingModeState {
  /** tabId → 思考选择（内存态，未设置的标签不占位） */
  strengthByTabId: Record<string, ThinkingStrength>
  /** 读取某标签选择（缺失回退 ''，惰性读 localStorage） */
  getStrength: (tabId: string) => ThinkingStrength
  /** 读取某标签的显式选择；未设置过（用户未选过）返回 ''。 */
  getExplicitStrength: (tabId: string) => ThinkingStrength | null
  /** 设置某标签选择（写内存 + 持久化） */
  setStrength: (tabId: string, strength: ThinkingStrength) => void
}

export const useThinkingModeStore = create<ThinkingModeState>((set, get) => ({
  strengthByTabId: {},

  getStrength: (tabId) => {
    const inMemory = get().strengthByTabId[tabId]
    if (inMemory) return inMemory
    // 惰性读 localStorage（首次访问该标签时恢复）
    const stored = loadStrength(tabId)
    if (stored) {
      set((state) => ({ strengthByTabId: { ...state.strengthByTabId, [tabId]: stored } }))
    }
    return stored
  },

  getExplicitStrength: (tabId) => {
    const inMemory = get().strengthByTabId[tabId]
    if (inMemory) return inMemory
    try {
      const raw = localStorage.getItem(getStorageKey(tabId))
      if (raw) return raw
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
 * 当前激活标签的显式思考选择（未设置过返回 null）。
 * 供 ChatContainer 组合：explicit（∈ 下发选项时生效）?? 端点当前参数组。
 */
export function useExplicitThinkingStrength(): ThinkingStrength | null {
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  return useThinkingModeStore((s) => (activeTabId ? s.getExplicitStrength(activeTabId) : null))
}
