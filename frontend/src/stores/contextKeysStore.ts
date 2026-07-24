/**
 * ContextKeysStore（ADR §3.4）
 *
 * 前端维护 context keys 状态，供 contributes 的 when 表达式求值。
 * 各事件（流水线启停、工作区聚焦、资源切换、交互挂起）通过 set* 方法更新。
 *
 * 基础集：
 * - pipeline.running / pipeline.idle        互斥（running=!idle）
 * - workspace.focus / chat.focus             工作区/聊天是否聚焦
 * - resource.isFile / resource.extname       当前资源
 * - interaction.pending                      是否有待处理交互
 */

import { create } from 'zustand'
import type { ContextKeys } from '@/services/schema/whenExpression'

/** 资源信息（setResource 入参） */
interface ResourceInfo {
  /** 是否为文件 */
  isFile: boolean
  /** 文件扩展名（含点号，如 '.py'） */
  extname: string
}

/** ContextKeysStore 状态与动作 */
interface ContextKeysState {
  /** 当前所有 context keys */
  keys: ContextKeys
  /** 读取单个 key */
  getKey: (key: string) => boolean | string | undefined
  /** 设置单个 key */
  setKey: (key: string, value: boolean | string) => void
  /** 批量设置 */
  setKeys: (patch: ContextKeys) => void
  /** 设置流水线运行状态（同时维护 running/idle 互斥） */
  setPipelineRunning: (running: boolean) => void
  /** 设置工作区聚焦 */
  setWorkspaceFocus: (focus: boolean) => void
  /** 设置聊天聚焦 */
  setChatFocus: (focus: boolean) => void
  /** 设置当前资源 */
  setResource: (info: ResourceInfo) => void
  /** 设置交互挂起状态 */
  setInteractionPending: (pending: boolean) => void
  /** 重置到默认状态 */
  reset: () => void
}

/** 默认 context keys：流水线空闲、无聚焦、无资源、无挂起交互 */
function defaultKeys(): ContextKeys {
  return {
    'pipeline.running': false,
    'pipeline.idle': true,
    'workspace.focus': false,
    'chat.focus': false,
    'resource.isFile': false,
    'resource.extname': '',
    'interaction.pending': false,
  }
}

export const useContextKeys = create<ContextKeysState>((set, get) => ({
  keys: defaultKeys(),

  getKey: (key) => get().keys[key],

  setKey: (key, value) =>
    set((state) => ({ keys: { ...state.keys, [key]: value } })),

  setKeys: (patch) => set((state) => ({ keys: { ...state.keys, ...patch } })),

  setPipelineRunning: (running) =>
    set((state) => ({
      keys: { ...state.keys, 'pipeline.running': running, 'pipeline.idle': !running },
    })),

  setWorkspaceFocus: (focus) =>
    set((state) => ({ keys: { ...state.keys, 'workspace.focus': focus } })),

  setChatFocus: (focus) =>
    set((state) => ({ keys: { ...state.keys, 'chat.focus': focus } })),

  setResource: (info) =>
    set((state) => ({
      keys: {
        ...state.keys,
        'resource.isFile': info.isFile,
        'resource.extname': info.extname,
      },
    })),

  setInteractionPending: (pending) =>
    set((state) => ({ keys: { ...state.keys, 'interaction.pending': pending } })),

  reset: () => set({ keys: defaultKeys() }),
}))
