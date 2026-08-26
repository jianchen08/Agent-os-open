/**
 * pending 输入队列 store（ADR-2026-08-26）
 *
 * 管道执行中发送的消息进入内核持久化队列（等待窗口内可修改/删除/清空），
 * 前端经 WS 事件 `pending_inputs_changed` 实时同步 + GET 端点对账恢复。
 * 消费（激活）时条目移入主消息流（stream_start → 现有流式协议），
 * 本 store 只承载"排队中"视图。
 */
import { create } from 'zustand'
import {
  clearPendingInputs,
  deletePendingInput,
  fetchPendingInputs,
  type PendingInputItem,
  updatePendingInput,
} from '@/services/api/pipelines'

interface PendingInputStore {
  /** pipelineId → 待处理条目（FIFO 序） */
  byPipeline: Record<string, PendingInputItem[]>
  /** 正在回填编辑的条目（pipelineId → inputId；空 = 无编辑态） */
  editingId: Record<string, string | null>

  /** GET 拉取对账（会话激活/刷新恢复时调用；已消费的条目后端已删，天然收敛） */
  load: (pipelineId: string) => Promise<void>
  /** WS 事件同步（enqueued/consumed/updated/deleted/cleared，payload 全量列表） */
  syncFromEvent: (pipelineId: string, items: PendingInputItem[]) => void
  /** PUT 修改（编辑保存） */
  updateContent: (pipelineId: string, inputId: string, content: string) => Promise<void>
  /** DELETE 单条 */
  remove: (pipelineId: string, inputId: string) => Promise<void>
  /** DELETE 全部 */
  clear: (pipelineId: string) => Promise<void>
  /** 编辑态切换（点击条目回填输入框） */
  setEditing: (pipelineId: string, inputId: string | null) => void
}

export const usePendingInputStore = create<PendingInputStore>((set, get) => ({
  byPipeline: {},
  editingId: {},

  load: async (pipelineId) => {
    if (!pipelineId) return
    try {
      const items = await fetchPendingInputs(pipelineId)
      set((state) => ({
        byPipeline: { ...state.byPipeline, [pipelineId]: items },
      }))
    } catch {
      // 拉取失败静默（对账兜底，非关键路径；事件仍会持续同步）
    }
  },

  syncFromEvent: (pipelineId, items) => {
    if (!pipelineId) return
    set((state) => ({
      byPipeline: { ...state.byPipeline, [pipelineId]: items },
      // 条目被消费/删除后清编辑态（避免编辑残留指向已不存在条目）
      editingId: {
        ...state.editingId,
        [pipelineId]: items.some((i) => i.id === state.editingId[pipelineId])
          ? state.editingId[pipelineId]
          : null,
      },
    }))
  },

  updateContent: async (pipelineId, inputId, content) => {
    await updatePendingInput(pipelineId, inputId, content)
    // 事件回推覆盖本地；本地乐观同步避免闪烁
    const items = get().byPipeline[pipelineId] ?? []
    set((state) => ({
      byPipeline: {
        ...state.byPipeline,
        [pipelineId]: items.map((i) => (i.id === inputId ? { ...i, content } : i)),
      },
      editingId: { ...state.editingId, [pipelineId]: null },
    }))
  },

  remove: async (pipelineId, inputId) => {
    await deletePendingInput(pipelineId, inputId)
    const items = get().byPipeline[pipelineId] ?? []
    set((state) => ({
      byPipeline: {
        ...state.byPipeline,
        [pipelineId]: items.filter((i) => i.id !== inputId),
      },
    }))
  },

  clear: async (pipelineId) => {
    await clearPendingInputs(pipelineId)
    set((state) => ({
      byPipeline: { ...state.byPipeline, [pipelineId]: [] },
      editingId: { ...state.editingId, [pipelineId]: null },
    }))
  },

  setEditing: (pipelineId, inputId) => {
    set((state) => ({
      editingId: { ...state.editingId, [pipelineId]: inputId },
    }))
  },
}))

/** 取某管道队列条数（组件渲染用；空管道返回 0） */
export function pendingInputCount(pipelineId: string | undefined | null): number {
  if (!pipelineId) return 0
  return usePendingInputStore.getState().byPipeline[pipelineId]?.length ?? 0
}
