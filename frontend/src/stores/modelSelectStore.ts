/**
 * 模型选择 Store（标签级独立记忆 + 路由联动）
 *
 * - 各对话标签（AgentTab）独立记忆显式模型选择，localStorage 持久化
 *   （key: model-select-{tabId}）；切换标签时选择随路由自动应用
 * - 选择值 = llm.yaml models 键（model_id），随 user_input 帧 model_id 字段
 *   透传——内核按 llm_core widget 声明的 frame_field 名单合成 state overlay，
 *   llm_core 解析链首位消费（显式选择最高优先级覆盖管道配置，2026-10-23
 *   用户裁定）；管线内绑定粘性，重复携带同值不换绑
 * - '' = 未选择（帧不带 model_id：后端按管线既有绑定/配置解析）
 */

import { create } from 'zustand'
import { useAgentTabStore } from '@/stores/agentTabStore'

/** localStorage 键前缀（按标签独立记忆） */
const STORAGE_KEY_PREFIX = 'model-select-'

function getStorageKey(tabId: string): string {
  return `${STORAGE_KEY_PREFIX}${tabId}`
}

/** 从 localStorage 惰性读选择，缺失回退 ''（= 未选择，消息不带模型字段） */
function loadModelId(tabId: string): string {
  try {
    return localStorage.getItem(getStorageKey(tabId)) ?? ''
  } catch {
    // storage 不可用（隐私模式等）→ 未选择
    return ''
  }
}

interface ModelSelectState {
  /** tabId → 显式模型选择（内存态，未设置的标签不占位） */
  modelByTabId: Record<string, string>
  /** 读取某标签选择（缺失回退 ''，惰性读 localStorage） */
  getModel: (tabId: string) => string
  /** 设置某标签选择（写内存 + 持久化；'' = 清除显式选择回到跟随配置） */
  setModel: (tabId: string, modelId: string) => void
}

export const useModelSelectStore = create<ModelSelectState>((set, get) => ({
  modelByTabId: {},

  getModel: (tabId) => {
    const inMemory = get().modelByTabId[tabId]
    if (inMemory) return inMemory
    const stored = loadModelId(tabId)
    if (stored) {
      set((state) => ({ modelByTabId: { ...state.modelByTabId, [tabId]: stored } }))
    }
    return stored
  },

  setModel: (tabId, modelId) => {
    try {
      if (modelId) {
        localStorage.setItem(getStorageKey(tabId), modelId)
      } else {
        localStorage.removeItem(getStorageKey(tabId))
      }
    } catch {
      // storage 不可用 → 仅内存态
    }
    set((state) => {
      const next = { ...state.modelByTabId, [tabId]: modelId }
      if (!modelId) delete next[tabId]
      return { modelByTabId: next }
    })
  },
}))

/**
 * 当前激活标签的显式模型选择（未设置过返回 ''）。
 * 供 ChatContainer 组合：显式选择随消息透传，未选择 = 跟随后端管线配置。
 */
export function useExplicitModelSelect(): string {
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  return useModelSelectStore((s) => (activeTabId ? s.getModel(activeTabId) : ''))
}
