/**
 * 交互卡片来源解析：把交互请求携带的来源坐标
 * （agentId/agentName/agentLevel/pipelineId/sessionId/threadId/createdAt）
 * 解析为人类可读的完整来源段（Agent(级别) · 管道 · 会话 · 时间）。
 * 纯读缓存函数，供浮层展示、全屏审批浮层与通知 sourceLabel 共用。
 *
 * 用户裁定（2026-10-01）：所有弹给用户的交互卡都必须显示完整来源——
 * 哪个 agent（名称+级别）、哪个会话、哪个管道（id+名称）、什么时间；
 * 后端 payload 缺权威名时回退前端缓存解析，再缺省该段不显示。
 */

import { readAgents } from '@/hooks/queries/useAgentsQuery'
import { readSessions } from '@/hooks/queries/useSessionsQuery'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import type { PendingInteraction } from '@/stores/interactionStore'

/** 来源解析输入（camelCase，与 PendingInteraction 对齐；raw 载荷先适配） */
export interface InteractionOriginInput {
  agentId?: string
  /** 发起 Agent 显示名（后端权威，缺省由缓存解析） */
  agentName?: string
  /** Agent 层级（L1/L2/L3） */
  agentLevel?: string
  pipelineId?: string
  sessionId?: string
  threadId?: string
  /** 后端创建时刻（ISO）；有值时来源行显示时分 */
  createdAt?: string
}

/** 解析后的来源四段（缺段为空串，渲染层跳过） */
export interface InteractionOriginDetail {
  /** "coding_dev_agent（L3）"；解析不到任何名字时空串 */
  agent: string
  /** "管道 pipe-xxx（名称）"；无 pipelineId 时空串 */
  pipeline: string
  /** 会话标题；缓存 miss 回退 threadId 原文 */
  session: string
  /** "HH:mm"；createdAt 缺失/非法时空串 */
  time: string
}

/** Agent 可读名：优先后端权威 agentName，其次管道元数据（sub_agent_created 下发），
 * 再次 agents 缓存按 agentId/configId 匹配；均缺且无管道坐标时回退 agentId 原文
 * （有管道坐标时 agentId 多为管道键冒充，不冒充 agent 名——管道段已可辨）。 */
function resolveAgentDisplayName(input: InteractionOriginInput): string {
  if (input.agentName) return input.agentName
  const pipelineMeta = input.pipelineId
    ? usePipelineMessageStore.getState().pipelines[input.pipelineId]
    : undefined
  if (pipelineMeta?.agentName) return pipelineMeta.agentName
  if (input.agentId) {
    const matched = readAgents().find(
      (a) => a.id === input.agentId || a.configId === input.agentId,
    )
    if (matched?.name) return matched.name
    if (!input.pipelineId) return input.agentId
  }
  return ''
}

/** 管道显示名：管道元数据的 agentName（管道名 = 运行该管道的 agent 名） */
function resolvePipelineDisplayName(pipelineId?: string): string {
  if (!pipelineId) return ''
  return usePipelineMessageStore.getState().pipelines[pipelineId]?.agentName || ''
}

/** 会话标题：payload sessionId → 管道元数据 sessionId → threadId，逐级查会话缓存 */
function resolveSessionTitle(input: InteractionOriginInput): string {
  const pipelineMeta = input.pipelineId
    ? usePipelineMessageStore.getState().pipelines[input.pipelineId]
    : undefined
  const sid = input.sessionId || pipelineMeta?.sessionId || input.threadId || ''
  if (!sid) return ''
  return readSessions().find((s) => s.id === sid)?.title || ''
}

/** ISO 时刻 → 本地 "HH:mm"；缺失/非法返回空串 */
export function formatOriginTime(createdAt?: string): string {
  if (!createdAt) return ''
  const d = new Date(createdAt)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

/** 交互请求 Agent 可读来源（通知 sourceLabel 用）：解析不到时回退 agentId 原文 */
export function resolveInteractionSourceLabel(parsed: {
  agentId: string
  pipelineId?: string
  agentName?: string
}): string {
  const named = resolveAgentDisplayName(parsed)
  if (named) return named
  return parsed.agentId || ''
}

/** 全量来源四段解析（交互卡/全屏审批浮层共用） */
export function resolveInteractionOriginDetail(
  input: InteractionOriginInput,
): InteractionOriginDetail {
  const agentName = resolveAgentDisplayName(input)
  const agent = agentName
    ? `${agentName}${input.agentLevel ? `（${input.agentLevel}）` : ''}`
    : ''
  const pipelineDisplayName = resolvePipelineDisplayName(input.pipelineId)
  const pipeline = input.pipelineId
    ? `管道 ${input.pipelineId}${pipelineDisplayName && pipelineDisplayName !== agentName ? `（${pipelineDisplayName}）` : ''}`
    : ''
  // 会话标题与 Agent 名同名时省略会话段（同行双份同名只增噪音，无辨析价值）；
  // 标题解析不到回退 threadId 原文（会话段 = 标题或 thread id，用户裁定两形态皆可）
  const sessionTitle = resolveSessionTitle(input)
  const session =
    sessionTitle && sessionTitle === agentName ? '' : sessionTitle || input.threadId || ''
  return { agent, pipeline, session, time: formatOriginTime(input.createdAt) }
}

/** 完整来源行文本：非空段去重后以 " · " 连接；全空返回空串（不显示来源行） */
export function formatInteractionOriginLabel(detail: InteractionOriginDetail): string {
  return [...new Set([detail.agent, detail.pipeline, detail.session, detail.time])].filter(Boolean).join(' · ')
}

/** 归属标签（PendingInteraction 入口）：Agent(级别) · 管道 · 会话 · 时间 */
export function resolveInteractionOriginLabel(
  interaction: Pick<
    PendingInteraction,
    'sessionId' | 'threadId' | 'agentId' | 'pipelineId'
  > &
    Partial<Pick<PendingInteraction, 'agentLevel' | 'createdAt'>> & { agentName?: string },
): string {
  return formatInteractionOriginLabel(resolveInteractionOriginDetail(interaction))
}

/** snake_case 原始事件载荷 → 来源解析输入（全屏审批浮层 approval.created 用） */
export function rawPayloadToOriginInput(payload: Record<string, unknown> | undefined): InteractionOriginInput {
  const p = payload || {}
  const str = (k: string) => (typeof p[k] === 'string' && p[k] ? (p[k] as string) : undefined)
  return {
    agentId: str('agent_id'),
    agentName: str('agent_name'),
    agentLevel: str('agent_level'),
    pipelineId: str('pipeline_id'),
    sessionId: str('session_id'),
    threadId: str('thread_id'),
    createdAt: str('created_at'),
  }
}
