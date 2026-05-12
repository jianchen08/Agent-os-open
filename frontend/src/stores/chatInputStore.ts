/**
 * ChatInput 插入文本的全局桥接 Store
 *
 * 解决跨组件向 ChatInput 插入文本的问题。
 * 外部组件（如 FileReviewTab）调用 requestInsert 写入待插入文本，
 * ChatInput 组件订阅 pendingInsert 消费后调用 consumeInsert 清空。
 */

import { create } from 'zustand'

interface ChatInputState {
  /** 待插入的文本（ChatInput 消费后清空） */
  pendingInsert: string | null
  /** 外部调用：请求向 ChatInput 插入文本 */
  requestInsert: (text: string) => void
  /** ChatInput 消费后调用：清除待插入 */
  consumeInsert: () => void
}

export const useChatInputStore = create<ChatInputState>((set) => ({
  pendingInsert: null,
  requestInsert: (text) => set({ pendingInsert: text }),
  consumeInsert: () => set({ pendingInsert: null }),
}))
