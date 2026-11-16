/**
 * usePipelineLiveUsage — 管道实时 token 用量推送通道的组件侧读面
 *
 * [来源: docs/decisions/2026-10-10-widget-action-bus.md §5] widget 数据面不与
 * 具体 store 绑死：cost_update / stream_end 事件驱动的逐管道用量（contextUsage
 * store，WS 推送面）经本 hook 暴露给 widget 层——推送通道无 HTTP 轮询形态，
 * 不走 datasourceUri 代理，与 useWidgetEventValue（widget_event 推送读面）
 * 同构。
 */

import { useContextUsageStore } from '@/stores/contextUsageStore'

/** 各管道最新实时用量（key 为 pipelineId；事件到达响应式重渲染） */
export function usePipelineLiveUsage() {
  return useContextUsageStore((s) => s.usageByPipeline)
}
