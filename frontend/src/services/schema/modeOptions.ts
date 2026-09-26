/**
 * 任务模式键契约与发送链并入（模式体系落地设计 §4.2 数据链）
 *
 * - 契约常量：任务上下文字段 `mode`，值 coding|writing|roleplay|research
 *   （「自动」= 不带键，模式归属由后端自然语言分类路径裁决）。
 * - 选项面（UI）不在本模块：任务模式选择器由 task_form 插件的 ui_schema
 *   form select 声明（id=task_mode，选项含图标写死于声明）+ 宿主受控桥渲染，
 *   与权限模式/思考强度选择器同构；模式面板徽标配对见 modePanel.ts。
 * - withTaskMode：发送时把所选模式并入消息级 execution_context（会话执行选项
 *   其余键保持原样），内核 1a2 合并点透传至任务上下文。
 * - withRoleplayPersona：roleplay 附身态并入消息级 execution_context（卡人设
 *   roleplay_persona + mode 强制 roleplay），主 agent 身份不变（工具面天然
 *   全量），人设经消息级上下文注入——不走 agent_id 身份切换（内核 WS 聊天
 *   入口不读该键，卡键还会连坐收窄工具面）。
 * - withSessionAgent / composeRoleplaySendIdentity：扮演会话绑定（roleplay
 *   .continue 桥落地的会话执行选项 agentId）并入 mode=roleplay + WS 帧
 *   agent_id（内核透传管道 state agent.id，卡身份=agent 键）；发送链身份
 *   三优先级：附身 > 扮演会话 > 不带，两路不同时注入。
 */

/** 模式键契约值（冻结，不得更改） */
export type TaskMode = 'coding' | 'writing' | 'roleplay' | 'research'

/** 模式键契约值集合 */
export const TASK_MODES: readonly TaskMode[] = ['coding', 'writing', 'roleplay', 'research']

/**
 * 模式键并入消息级 execution_context
 *
 * 「自动」（mode 缺席）不带键——原 execution_context 原样返回（含 undefined）；
 * 显式选择时浅合并出新的 context（不改动会话执行选项快照对象）。
 */
export function withTaskMode(
  executionContext: Record<string, unknown> | undefined,
  mode: TaskMode | undefined,
): Record<string, unknown> | undefined {
  if (!mode) return executionContext
  return { ...executionContext, mode }
}

/**
 * 角色扮演附身档（possess 链的共享契约）：roleplay 面板附身卡成功后经
 * roleplay.possess 宿主桥上行，由 roleplayPossessStore 持有；发送链按持有态
 * 经 withRoleplayPersona 注入消息级 execution_context，解除即不带。
 */
export interface RoleplayPossession {
  /** 卡 id（模式包 agents/card_*.yaml 的文件名去后缀，如 card_luna） */
  card_id: string
  /** 卡显示名（输入区指示条展示用） */
  name: string
  /** 卡 avatar：emoji 串或 {fg,bg} 色对（卡画廊同款两形态，桥载荷校验同口径） */
  avatar: string | { fg: string; bg: string }
  /** 卡人设文本（面板以 description+personality+scenario 拼接上行；空串容许） */
  personaText: string
}

/**
 * 附身态并入消息级 execution_context：附身存在 → 浅合并出带 roleplay_persona
 * （卡人设）+ mode=roleplay（附身即激活模式物料，覆写既有 mode 键）的新对象；
 * 无附身原样返回（含 undefined）。
 */
export function withRoleplayPersona(
  executionContext: Record<string, unknown> | undefined,
  possessed: RoleplayPossession | null,
): Record<string, unknown> | undefined {
  if (!possessed) return executionContext
  return { ...executionContext, roleplay_persona: possessed.personaText, mode: 'roleplay' }
}

/**
 * 扮演会话绑定并入消息级 execution_context：会话执行选项绑定 agentId
 * （roleplay.continue 桥落地的「扮演会话」）→ 浅合并出 mode=roleplay（扮演
 * 会话即激活模式物料，覆写既有 mode 键）的新对象；无绑定原样返回（含
 * undefined）。agent_id 本身走 WS 帧 agent_id 参数（内核透传管道 state
 * agent.id，卡 yaml 窄工具面生效），不经 execution_context。可选开演档键
 * （roleplay_greeting 所选开场白 / roleplay_user_persona 用户设定，会话化
 * 开演经执行选项快照逐消息透传）非空时一并并入，material.py 据此组注入段。
 */
export function withSessionAgent(
  executionContext: Record<string, unknown> | undefined,
  agentId: string | undefined,
  roleplayGreeting?: string,
  roleplayUserPersona?: string,
): Record<string, unknown> | undefined {
  if (!agentId) return executionContext
  return {
    ...executionContext,
    mode: 'roleplay',
    ...(roleplayGreeting?.trim() ? { roleplay_greeting: roleplayGreeting } : {}),
    ...(roleplayUserPersona?.trim() ? { roleplay_user_persona: roleplayUserPersona } : {}),
  }
}

/**
 * 发送链扮演身份归一（三优先级：附身 > 扮演会话绑定 > 不带）。
 *
 * 附身是用户显式全局态，与扮演会话并存时附身独占——WS agent_id 与
 * execution_context 两路全让位，保证不同时注入 roleplay_persona 与 agent_id
 * （两键的卡键优先序已在后端 material.py 定死，前端只走单路）。agentId 缺席
 * 表示本帧不带身份（GlobalWebSocket 对空值不下发 agent_id 键）。开演档键
 * （开场白/用户设定）仅在扮演会话路并入，附身路不消费。
 */
export function composeRoleplaySendIdentity(
  executionContext: Record<string, unknown> | undefined,
  possessed: RoleplayPossession | null,
  sessionAgentId: string | undefined,
  roleplayGreeting?: string,
  roleplayUserPersona?: string,
): { executionContext: Record<string, unknown> | undefined; agentId: string | undefined } {
  if (possessed) {
    return { executionContext: withRoleplayPersona(executionContext, possessed), agentId: undefined }
  }
  return {
    executionContext: withSessionAgent(executionContext, sessionAgentId, roleplayGreeting, roleplayUserPersona),
    agentId: sessionAgentId,
  }
}
