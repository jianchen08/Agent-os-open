/**
 * ChatInput 全局桥接 Store
 *
 * 职责一：解决跨组件向 ChatInput 插入文本的问题。
 * 外部组件（如 CodeEditor 的选中引用浮动按钮）调用 requestInsert 写入待插入文本，
 * ChatInput 组件订阅 pendingInsert 消费后调用 consumeInsert 清空。
 *
 * 职责二：草稿保存功能。
 * 当用户在不同会话/Tab之间切换时，输入框中的未发送文本通过 drafts 状态保留，
 * 切回来时恢复之前输入的内容。
 */

import { create } from 'zustand'

interface ChatInputState {
  /** 待插入的文本（ChatInput 消费后清空） */
  pendingInsert: string | null
  /** 按 tabId/sessionId 存储的草稿文本 */
  drafts: Record<string, string>
  /** 外部调用：请求向 ChatInput 插入文本 */
  requestInsert: (text: string) => void
  /** ChatInput 消费后调用：清除待插入 */
  consumeInsert: () => void
  /** 保存草稿文本到指定 key */
  saveDraft: (key: string, text: string) => void
  /** 加载指定 key 的草稿文本，不存在则返回空字符串 */
  loadDraft: (key: string) => string
  /** 清除指定 key 的草稿文本 */
  clearDraft: (key: string) => void
}

export const useChatInputStore = create<ChatInputState>((set, get) => ({
  pendingInsert: null,
  drafts: {},

  requestInsert: (text) => set({ pendingInsert: text }),
  consumeInsert: () => set({ pendingInsert: null }),

  /**
   * 保存草稿文本到指定 key
   */
  saveDraft: (key, text) => {
    set((state) => ({
      drafts: { ...state.drafts, [key]: text },
    }))
  },

  /**
   * 加载指定 key 的草稿文本
   */
  loadDraft: (key) => {
    return get().drafts[key] || ''
  },

  /**
   * 清除指定 key 的草稿文本
   */
  clearDraft: (key) => {
    set((state) => {
      const { [key]: _, ...rest } = state.drafts
      return { drafts: rest }
    })
  },
}))
