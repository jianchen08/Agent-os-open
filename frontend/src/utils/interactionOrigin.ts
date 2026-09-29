/**
 * 交互卡片来源解析：把 PendingInteraction 携带的坐标
 * （sessionId/threadId/pipelineId/agentId）解析为人类可读的归属标签
 * （会话标题 · Agent/管道名）。纯读缓存函数，供浮层展示与通知 sourceLabel 共用。
 */

import { readAgents } from '@/hooks/queries/useAgentsQuery'
import { readSessions } from '@/hooks/queries/useSessionsQuery'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import type { PendingInteraction } from '@/stores/interactionStore'

/**
 * 交互请求的 Agent 可读来源：优先管道元数据里的 Agent 名称（sub_agent_created
 * 事件下发），其次 agents 缓存按 agentId/configId 匹配，最后回退 agentId 原文。
 * 解析不到时返回空串（渲染层不显示来源标签）。
 */
export function resolveInteractionSourceLabel(parsed: {
  agentId: string
  pipelineId?: string
}): string {
  const pipelineMeta = parsed.pipelineId
    ? usePipelineMessageStore.getState().pipelines[parsed.pipelineId]
    : undefined
  if (pipelineMeta?.agentName) return pipelineMeta.agentName
  if (parsed.agentId) {
    const matched = readAgents().find(
      (a) => a.id === parsed.agentId || a.configId === parsed.agentId,
    )
    return matched?.name || parsed.agentId
  }
  return ''
}

/** 交互归属会话标题：sessionId → threadId 依次匹配会话缓存；解析不到返回空串 */
export function resolveInteractionSessionTitle(interaction: {
  sessionId?: string
  threadId: string
}): string {
  const sid = interaction.sessionId || interaction.threadId
  if (!sid) return ''
  return readSessions().find((s) => s.id === sid)?.title || ''
}

/** 归属标签：会话标题 · Agent/管道名（同名去重；两者皆缺返回空串不显示） */
export function resolveInteractionOriginLabel(
  interaction: Pick<PendingInteraction, 'sessionId' | 'threadId' | 'agentId' | 'pipelineId'>,
): string {
  const parts = [
    resolveInteractionSessionTitle(interaction),
    resolveInteractionSourceLabel(interaction),
  ].filter(Boolean)
  return [...new Set(parts)].join(' · ')
}
