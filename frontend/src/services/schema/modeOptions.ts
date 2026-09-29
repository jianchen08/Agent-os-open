/**
 * 任务模式键契约与发送链并入（模式体系落地设计 §4.2 数据链）
 *
 * - 契约：任务上下文字段 `mode` 是**开放标签**（D1 标签裁定：值域 = registry
 *   扫描真值集，非枚举）——本模块零冻结键集；「默认」= 不带键，主 agent 按模式
 *   目录（{{mode_catalog}}）自行归类派发（2026-09-28 设计 D4.1；后端分类器
 *   不实现）。选择器选项由 registry 派生（taskModeOptionsFromModes，modes.ts）。
 * - withTaskMode：发送时把所选模式并入消息级 execution_context（会话执行选项
 *   其余键保持原样），内核 1a2 合并点透传至任务上下文。
 * - withPossession：附身态（persona 接管，D7 通用机制）并入消息级
 *   execution_context——注入键随附身档携带（建立时从 registry 该模式
 *   decl.persona.from 解析一次，出生即钉；未解析=诚实降级不注入），主 agent
 *   身份不变（工具面天然全量），不走 agent_id 身份切换（内核 WS 聊天入口不读
 *   该键，卡键还会连坐收窄工具面）。
 * - withSessionBinding / composeSendIdentity：模式会话绑定（modeSessionBinder
 *   出生通道写入会话执行选项快照的 agentId + modeBinding + 扩展上下文）并入
 *   mode 键 + WS 帧 agent_id（内核透传管道 state agent.id，agent 身份=agent
 *   键）；发送链身份三优先级：附身 > 会话绑定 > 不带，两路不同时注入。
 */

/** mode 键形态守卫（与后端 MODE_ID_RE 同口径：`[a-z][a-z0-9_]{0,63}`）——
 *  桥载荷携带的 mode 键经此校验，防凭空键流入会话绑定面 */
const MODE_ID_SHAPE_RE = /^[a-z][a-z0-9_]{0,63}$/

/** mode 键是否为合法形态（标签值域开放，本守卫只把形态关） */
export function isModeIdShape(value: unknown): value is string {
  return typeof value === 'string' && MODE_ID_SHAPE_RE.test(value)
}

/**
 * 模式键并入消息级 execution_context
 *
 * 「默认」（mode 缺席）不带键——原 execution_context 原样返回（含 undefined）；
 * 显式选择时浅合并出新的 context（不改动会话执行选项快照对象）。
 */
export function withTaskMode(
  executionContext: Record<string, unknown> | undefined,
  mode: string | undefined,
): Record<string, unknown> | undefined {
  if (!mode) return executionContext
  return { ...executionContext, mode }
}

/**
 * 附身档（persona 接管链的共享契约）：模式面板附身成功后经 mode.possess 宿主
 * 桥上行，由 personaPossessStore 持有；发送链按持有态经 withPossession 注入
 * 消息级 execution_context，解除即不带。
 */
export interface PersonaPossession {
  /** 附身归属模式键（注入 mode=<该键>；桥载荷携带，形态守卫把关） */
  mode: string
  /** 卡/人设源 id（模式包 agents 命名空间内的档案名，展示用） */
  card_id: string
  /** 显示名（输入区指示条展示用） */
  name: string
  /** avatar：emoji 串或 {fg,bg} 色对（卡画廊同款两形态，桥载荷校验同口径） */
  avatar: string | { fg: string; bg: string }
  /** 人设文本（面板拼接上行；空串容许——模式键仍生效） */
  personaText: string
  /** 人设注入键（建立时从 registry decl.persona.from 解析一次随档钉住；
   *  null = 未解析（registry 不可达）→ 诚实降级：发送链不注入附身） */
  personaKey: string | null
}

/**
 * 附身态并入消息级 execution_context：附身存在且注入键已解析 → 浅合并出
 * `[personaKey]: personaText`（人设）+ mode=<附身模式键>（附身即激活模式物料，
 * 覆写既有 mode 键）的新对象；无附身或键未解析原样返回（含 undefined）。
 */
export function withPossession(
  executionContext: Record<string, unknown> | undefined,
  possessed: PersonaPossession | null,
): Record<string, unknown> | undefined {
  if (!possessed?.personaKey) return executionContext
  return {
    ...executionContext,
    [possessed.personaKey]: possessed.personaText,
    mode: possessed.mode,
  }
}

/** 模式会话绑定（modeSessionBinder 出生通道写入会话执行选项快照） */
export interface SessionBinding {
  /** 会话出生即定的模式键（快照 modeBinding.mode；空串 = 会话无模式绑定） */
  mode: string
  /** 绑定执行者 agent 键（缺省 = 主 agent 身份） */
  agentId?: string
  /** 随会话逐消息并入 execution_context 的扩展键（开演档等，键值非空） */
  extraContext?: Record<string, string>
}

/**
 * 会话绑定并入消息级 execution_context：绑定在场 → 浅合并出 mode=<绑定模式
 * 键>（会话即激活模式物料，覆写既有 mode 键；空串不带）+ 扩展上下文键的新对象；
 * 无绑定原样返回（含 undefined）。agent_id 本身走 WS 帧 agent_id 参数（内核
 * 透传管道 state agent.id，卡 yaml 窄工具面生效），不经 execution_context。
 */
export function withSessionBinding(
  executionContext: Record<string, unknown> | undefined,
  binding: SessionBinding | undefined,
): Record<string, unknown> | undefined {
  if (!binding) return executionContext
  return {
    ...executionContext,
    ...(binding.mode ? { mode: binding.mode } : {}),
    ...(binding.extraContext ?? {}),
  }
}

/**
 * 会话模式绑定并入（2026-09-28 设计 D1/D2，会话出生即定）：快照 modeBinding
 * 的 mode 为唯一模式身份键，覆写消息级既有 mode（会话身份恒定，强于选择器
 * 与其他消息级来源）；pipelineConfigId 不进 execution_context——它走 WS 帧
 * 顶层 pipeline_config_id（发送链直接读快照）。无绑定原样返回。
 */
export function withModeBinding(
  executionContext: Record<string, unknown> | undefined,
  binding: { mode: string; pipelineConfigId?: string } | undefined,
): Record<string, unknown> | undefined {
  if (!binding?.mode) return executionContext
  return { ...executionContext, mode: binding.mode }
}

/**
 * 发送链身份归一（三优先级：附身 > 会话绑定 > 不带）。
 *
 * 附身是用户显式全局态，与会话绑定并存时附身独占——WS agent_id 与
 * execution_context 两路全让位，保证不同时注入人设键与 agent_id（两键的
 * 卡键优先序已在后端 material.py 定死，前端只走单路）。附身注入键未解析
 * （registry 不可达）= 附身对发送链不可用，回落会话绑定路（诚实降级：无键
 * 附身不阻断会话身份注入）。agentId 缺席表示本帧不带身份（GlobalWebSocket
 * 对空值不下发 agent_id 键）。扩展上下文键仅会话绑定路并入，附身路不消费。
 */
export function composeSendIdentity(
  executionContext: Record<string, unknown> | undefined,
  possessed: PersonaPossession | null,
  sessionBinding: SessionBinding | undefined,
): { executionContext: Record<string, unknown> | undefined; agentId: string | undefined } {
  if (possessed?.personaKey) {
    return { executionContext: withPossession(executionContext, possessed), agentId: undefined }
  }
  return {
    executionContext: withSessionBinding(executionContext, sessionBinding),
    agentId: sessionBinding?.agentId,
  }
}
