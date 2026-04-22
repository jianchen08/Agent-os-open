/**
 * 消息版本管理 Store
 *
 * 负责消息版本的创建、恢复、查询和比较功能
 * 实现消息重试机制中的版本管理
 */

import { create } from 'zustand'
import type {
  Message,
  MessageVersion,
  VersionDiff,
  MessageToolCall,
} from '@/types/models'

/**
 * 消息版本状态接口
 */
interface MessageVersionState {
  /** 消息版本映射 {messageId: MessageVersion[]} */
  versions: Record<string, MessageVersion[]>
  /** 消息当前版本号映射 {messageId: version} */
  currentVersions: Record<string, number>

  /**
   * 创建消息版本快照
   * @param message 消息对象
   * @returns 版本号
   */
  createVersion: (message: Message) => number

  /**
   * 获取消息的所有版本
   * @param messageId 消息 ID
   * @returns 版本列表
   */
  getVersions: (messageId: string) => MessageVersion[]

  /**
   * 恢复到指定版本
   * @param messageId 消息 ID
   * @param version 版本号
   * @returns 版本快照的消息数据
   */
  restoreVersion: (messageId: string, version: number) => Message | null

  /**
   * 比较两个版本
   * @param messageId 消息 ID
   * @param version1 版本号 1
   * @param version2 版本号 2
   * @returns 版本差异
   */
  compareVersions: (
    messageId: string,
    version1: number,
    version2: number
  ) => VersionDiff | null

  /**
   * 删除消息的所有版本
   * @param messageId 消息 ID
   */
  clearVersions: (messageId: string) => void

  /**
   * 获取指定版本号的消息快照
   * @param messageId 消息 ID
   * @param version 版本号
   * @returns 消息快照或 null
   */
  getVersionSnapshot: (messageId: string, version: number) => Message | null
}

/**
 * 比较工具调用列表差异
 */
const compareToolCalls = (
  toolCalls1: MessageToolCall[],
  toolCalls2: MessageToolCall[]
): VersionDiff['toolCallsChanged'] => {
  const map1 = new Map(toolCalls1.map(tc => [tc.call_id, tc]))
  const map2 = new Map(toolCalls2.map(tc => [tc.call_id, tc]))

  const added: string[] = []
  const removed: string[] = []
  const modified: string[] = []

  // 检查新增和修改
  for (const [id, tc2] of map2) {
    const tc1 = map1.get(id)
    if (!tc1) {
      added.push(id)
    } else if (
      tc1.status !== tc2.status ||
      tc1.result !== tc2.result ||
      JSON.stringify(tc1.tool_args) !== JSON.stringify(tc2.tool_args)
    ) {
      modified.push(id)
    }
  }

  // 检查删除
  for (const [id] of map1) {
    if (!map2.has(id)) {
      removed.push(id)
    }
  }

  return { added, removed, modified }
}

/**
 * 消息版本管理 Store
 */
export const useMessageVersionStore = create<MessageVersionState>(
  (set, get) => ({
    versions: {},
    currentVersions: {},

    /**
     * 创建消息版本快照
     */
    createVersion: (message: Message) => {
      const state = get()
      const messageVersions = state.versions[message.id] || []
      const currentVersion = state.currentVersions[message.id] || 0

      // 生成新版本号
      const newVersion = currentVersion + 1

      // 创建版本快照
      const version: MessageVersion = {
        version: newVersion,
        content: message.content,
        toolCalls: message.toolCalls || [],
        createdAt: new Date().toISOString(),
        isCurrent: true,
        messageSnapshot: message,
      }

      // 更新旧版本的 isCurrent 标记
      const updatedVersions = messageVersions.map(v => ({
        ...v,
        isCurrent: false,
      }))

      // 保存新版本
      set(state => ({
        versions: {
          ...state.versions,
          [message.id]: [...updatedVersions, version],
        },
        currentVersions: {
          ...state.currentVersions,
          [message.id]: newVersion,
        },
      }))

      return newVersion
    },

    /**
     * 获取消息的所有版本
     */
    getVersions: (messageId: string) => {
      const state = get()
      return state.versions[messageId] || []
    },

    /**
     * 恢复到指定版本
     */
    restoreVersion: (messageId: string, version: number) => {
      const state = get()
      const versions = state.versions[messageId]

      if (!versions) {
        console.warn(`[messageVersionStore] 消息 ${messageId} 没有版本记录`)
        return null
      }

      const targetVersion = versions.find(v => v.version === version)

      if (!targetVersion) {
        console.warn(
          `[messageVersionStore] 消息 ${messageId} 没有版本 ${version}`
        )
        return null
      }

      // 更新所有版本的 isCurrent 标记
      const updatedVersions = versions.map(v => ({
        ...v,
        isCurrent: v.version === version,
      }))

      // 更新状态
      set(state => ({
        versions: {
          ...state.versions,
          [messageId]: updatedVersions,
        },
        currentVersions: {
          ...state.currentVersions,
          [messageId]: version,
        },
      }))

      // 返回快照的消息数据
      return {
        ...targetVersion.messageSnapshot,
        id: messageId, // 确保使用原消息 ID
      } as Message
    },

    /**
     * 比较两个版本
     */
    compareVersions: (
      messageId: string,
      version1: number,
      version2: number
    ) => {
      const state = get()
      const versions = state.versions[messageId]

      if (!versions) {
        return null
      }

      const v1 = versions.find(v => v.version === version1)
      const v2 = versions.find(v => v.version === version2)

      if (!v1 || !v2) {
        return null
      }

      // 比较内容
      const contentChanged = v1.content !== v2.content

      // 比较工具调用
      const toolCallsChanged = compareToolCalls(v1.toolCalls, v2.toolCalls)

      return {
        contentChanged,
        toolCallsChanged,
      }
    },

    /**
     * 删除消息的所有版本
     */
    clearVersions: (messageId: string) => {
      set(state => {
        const newVersions = { ...state.versions }
        const newCurrentVersions = { ...state.currentVersions }

        delete newVersions[messageId]
        delete newCurrentVersions[messageId]

        return {
          versions: newVersions,
          currentVersions: newCurrentVersions,
        }
      })
    },

    /**
     * 获取指定版本号的消息快照
     */
    getVersionSnapshot: (messageId: string, version: number) => {
      const state = get()
      const versions = state.versions[messageId]

      if (!versions) {
        return null
      }

      const targetVersion = versions.find(v => v.version === version)

      if (!targetVersion) {
        return null
      }

      return {
        ...targetVersion.messageSnapshot,
        id: messageId,
      } as Message
    },
  })
)
