/**
 * 扮演会话绑定（roleplay.continue 桥的宿主侧落地面）
 *
 * 「扮演会话」= 普通聊天会话 + 会话执行选项绑定 agentId（mode_roleplay/<card_id>，
 * 卡人设 id 即 agent 键）：创建会话（标题 扮演·<卡名>，store 创建流随建即激活
 * = 已切到聊天区）后把卡身份写进该会话的执行选项快照。发送链（router.tsx
 * composeRoleplaySendIdentity）据此逐消息并入 WS 帧 agent_id（内核透传管道
 * state agent.id，卡 yaml 窄工具面生效）+ mode=roleplay。
 *
 * 会话化开演（2026-09-25，扮演是对话不是任务）：载荷可另携所选开场白与用户
 * 设定文本，随绑定落入会话执行选项快照（发送链逐消息并入 execution_context
 * .roleplay_greeting / roleplay_user_persona，material.py 组装进注入块）；创建
 * 完成后宿主自动发送一条极短触发消息，AI 据开场白键以所选开场演出——「新开
 * 扮演会话」点击即建会话即开演，全程无任务派发。
 */

import { withSessionAgent } from '@/services/schema/modeOptions'
import {
  loadSessionExecutionOptions,
  saveSessionExecutionOptions,
} from '@/services/sessionExecutionOptions'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useSessionListStore } from '@/stores/sessionListStore'
import { generateUUID } from '@/utils/uuid'

/** roleplay.continue 桥载荷：card_id/name 必填（fail-closed）；avatar 为与
 *  roleplay.possess 对称的装饰位，宿主创建流不消费（指示条用 name）。可选
 *  greeting/personaText 为会话化开演档（fresh）携带：所选开场白与用户设定
 *  原文；转续演缺省（纯净会话）。 */
export interface RoleplayContinuePayload {
  card_id: string
  name: string
  greeting?: string
  personaText?: string
}

/** 会话执行选项里的扮演绑定读出形态（name 缺席回退 agentId 尾段） */
export interface SessionAgentBinding {
  agentId: string
  name: string
}

/** 卡身份的 agent 键命名空间（模式包 agents/card_*.yaml，§8.1 卡=模式命名空间） */
const CARD_AGENT_PREFIX = 'mode_roleplay/'

/** 会话化开演的自动首条触发消息：极短中性触发，AI 据执行选项快照的开场白键
 *  以所选开场演出（角色视角开场，用户消息本身不进剧情）。 */
const FIRST_TRIGGER_MESSAGE = '（开始）'

/**
 * 载荷 → 绑定载荷：card_id/name 非空字符串为必要项；greeting/personaText
 * 缺省合法，在场时必须是非空字符串——非法一律返回 null（桥侧 fail-closed：
 * 整包丢弃零状态变更并回 error）。
 */
export function parseRoleplayContinuePayload(payload: unknown): RoleplayContinuePayload | null {
  if (typeof payload !== 'object' || payload === null) return null
  const p = payload as Record<string, unknown>
  if (typeof p.card_id !== 'string' || !p.card_id.trim()) return null
  if (typeof p.name !== 'string' || !p.name.trim()) return null
  const parsed: RoleplayContinuePayload = { card_id: p.card_id, name: p.name }
  for (const key of ['greeting', 'personaText'] as const) {
    const value = p[key]
    if (value === undefined) continue
    if (typeof value !== 'string' || !value.trim()) return null
    parsed[key] = value
  }
  return parsed
}

/**
 * 以卡身份开纯净扮演会话：建会话（store 编排激活/标签/主管道注册）→ 该会话
 * 执行选项写入绑定与开演档（开场白/用户设定，既有快照原样保留其余键，无快照
 * 落最小包）→ 自动发送首条触发消息（靠快照键让 AI 以所选开场白演出）。
 * 返回回执供桥 result 下行（含 sessionId）。
 */
export async function createRoleplaySession(
  payload: RoleplayContinuePayload,
): Promise<{ sessionId: string; agentId: string; name: string }> {
  const agentId = `${CARD_AGENT_PREFIX}${payload.card_id}`
  const created = await useSessionListStore
    .getState()
    .createSession(`扮演·${payload.name}`)
  const snapshot = loadSessionExecutionOptions(created.id)
  saveSessionExecutionOptions(created.id, {
    values: snapshot?.values ?? {},
    executionContext: snapshot?.executionContext,
    agentId,
    agentName: payload.name,
    ...(payload.greeting ? { roleplayGreeting: payload.greeting } : {}),
    ...(payload.personaText ? { roleplayUserPersona: payload.personaText } : {}),
  })
  // 自动首条（创建→绑定→切换→开演）：execution_context 复用发送链同一并入
  // 形态（withSessionAgent），保证首条与后续消息注入口径一致。
  globalWS.sendUserInput(created.id, FIRST_TRIGGER_MESSAGE, {
    pipelineId: created.pipelineIds?.[0],
    clientMessageId: generateUUID(),
    executionContext: withSessionAgent(undefined, agentId, payload.greeting, payload.personaText),
    agentId,
  })
  return { sessionId: created.id, agentId, name: payload.name }
}

/** 读会话的扮演绑定；无绑定返回 null（指示条不渲染） */
export function readSessionAgent(threadId: string): SessionAgentBinding | null {
  const snapshot = loadSessionExecutionOptions(threadId)
  if (!snapshot?.agentId) return null
  const slash = snapshot.agentId.indexOf('/')
  const tail = slash >= 0 ? snapshot.agentId.slice(slash + 1) : snapshot.agentId
  return { agentId: snapshot.agentId, name: snapshot.agentName || tail }
}

/** 退出扮演会话：清该会话执行选项的绑定键，其余快照键原样保留 */
export function clearSessionAgent(threadId: string): void {
  const snapshot = loadSessionExecutionOptions(threadId)
  if (!snapshot) return
  saveSessionExecutionOptions(threadId, { ...snapshot, agentId: undefined, agentName: undefined })
}
