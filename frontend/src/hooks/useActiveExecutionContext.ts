/**
 * 执行上下文读面 — widget 层消费「当前会话/当前管道」的平台数据契约
 *
 * [来源: docs/decisions/2026-10-10-widget-action-bus.md §5] agent_id 的本质是
 * 执行上下文键（内核零 agent 运行时知识）；前端侧「当前活跃会话 / 活跃管道
 * 标签」同属宿主持有的执行上下文状态。widget 数据面不与具体 store 绑死——
 * 经本模块暴露读面（响应式 hook + 非响应式 getter），widget 只见语义，
 * 不感知 agentTabStore / pipelineMessageStore / sessionStore 的存储实现。
 *
 * 与 hooks/queries/* 同为组件标准数据入口（OBS-R258-1 四态约定的入口层先例）。
 */

import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'

/** 当前活跃会话 id（无活跃会话为 null；切会话响应式重渲染） */
export function useActiveSessionId(): string | null {
  return useSessionStore((s) => s.activeSessionId)
}

/**
 * 非响应式读当前活跃会话 id（事件回调/异步流程中取即时值用；
 * 渲染面一律用 useActiveSessionId）
 */
export function readActiveSessionId(): string | null {
  return useSessionStore.getState().activeSessionId
}

/**
 * 当前执行管道坐标（表单端点附加上文等提交链用）：
 * 活跃 Tab 的 pipelineRunId 优先，回退管道消息 store 级 activePipelineId，
 * 均无 → 空串（后端端点对空值显式处置）。
 *
 * 会话 id 是组织集合 id，绝不兜底进管道坐标（2026-08-30 管道身份裁定）。
 */
export function useActivePipelineId(): string {
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  const tabs = useAgentTabStore((s) => s.tabs)
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)
  const tabPipelineId = tabs.find((t) => t.id === activeTabId)?.pipelineRunId
  return tabPipelineId ?? activePipelineId ?? ''
}

/**
 * 仅按活跃 Tab 取管道坐标（不回退 store 级 activePipelineId）：
 * Tab 无 pipelineRunId（新会话首轮前）→ 空串。消费方需要「严格跟随标签、
 * 不吃跨 Tab 残留」的口径时用本变体（如输入框用量指示器按当前标签挑
 * state 行，回退会把上一管道的行误显示进来）。
 */
export function useActiveTabPipelineId(): string {
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  const tabs = useAgentTabStore((s) => s.tabs)
  return tabs.find((t) => t.id === activeTabId)?.pipelineRunId ?? ''
}
