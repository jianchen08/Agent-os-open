/** 任务行级写操作进行中订阅（#11 统一暴露的消费面）。
 *
 * 独立成 hooks 件的原因：widgets 目录禁 import '@/stores'（ADR 2026-10-10
 * widget Action Bus，eslint local-rules/no-widget-store-imports）——任务面板
 * 行按钮/确认弹窗的行级 pending 订阅经本 hook 转介，真值源 =
 * longTermTaskStore.actingTaskIds（与 store 写动作/服务编排同源）。 */
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'

/** 该任务是否处于行级写操作 await 窗口（pause/resume/cancel/toggle 进行中） */
export function useTaskActing(taskId: string | null | undefined): boolean {
  return useLongTermTaskStore((s) => (taskId ? s.actingTaskIds.has(taskId) : false))
}
