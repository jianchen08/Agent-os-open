/**
 * pending 输入队列 store（ADR-2026-08-26）
 *
 * 管道执行中发送的消息进入内核持久化队列（等待窗口内可删除/清空；
 * 2026-10-19 用户裁定：条目点击 = 出队退回输入框，条目内编辑态删除），
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
} from '@/services/api/pipelines'
import { useChatInputStore } from '@/stores/chatInputStore'

interface PendingInputStore {
  /** pipelineId → 待处理条目（FIFO 序） */
  byPipeline: Record<string, PendingInputItem[]>

  /** GET 拉取对账（会话激活/刷新恢复/轮次收尾兜底时调用；已消费的条目后端已删，天然收敛） */
  load: (pipelineId: string) => Promise<void>
  /** WS 事件同步（enqueued/consumed/updated/deleted/cleared/returned，payload 全量列表） */
  syncFromEvent: (pipelineId: string, items: PendingInputItem[]) => void
  /** DELETE 单条 */
  remove: (pipelineId: string, inputId: string) => Promise<void>
  /** DELETE 全部 */
  clear: (pipelineId: string) => Promise<void>
}

/** 队列条出现即落定：条目携带的 client_message_id 命中在途登记 → 撤发送态
 *  （未知 cmid 在 chatInputStore 内幂等零动作，注入消息不误触） */
function landSendsByQueueItems(items: PendingInputItem[]): void {
  for (const item of items) {
    if (item.client_message_id) {
      useChatInputStore.getState().landSendByCmid(item.client_message_id)
    }
  }
}

export const usePendingInputStore = create<PendingInputStore>((set, get) => ({
  byPipeline: {},

  load: async (pipelineId) => {
    if (!pipelineId) return
    try {
      const items = await fetchPendingInputs(pipelineId)
      set((state) => ({
        byPipeline: { ...state.byPipeline, [pipelineId]: items },
      }))
      // 队列条权威视图更新 = 落定点（GET 对账兜底通道，事件漏接窗口由此收敛）
      landSendsByQueueItems(items)
    } catch {
      // 拉取失败静默（对账兜底，非关键路径；事件仍会持续同步）
    }
  },

  syncFromEvent: (pipelineId, items) => {
    if (!pipelineId) return
    set((state) => ({
      byPipeline: { ...state.byPipeline, [pipelineId]: items },
    }))
    // 排队落定通道（2026-11-04 落定语义重定义）：队列条出现即为落点——条目
    // 携带 client_message_id（user 来源非空；trigger/task 来源为空串天然跳过）
    // 且在途登记命中 → 按 cmid 撤发送态。非 enqueued 动作的条目同视图同语义。
    landSendsByQueueItems(items)
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
    }))
  },
}))
