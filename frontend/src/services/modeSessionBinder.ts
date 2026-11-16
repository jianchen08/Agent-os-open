/**
 * 模式会话出生通道（设计 D0 步1 / §3.2 批 G④，选择器与面板桥共用）
 *
 * 「模式会话」= 普通聊天会话 + 会话执行选项快照写 modeBinding（`mode` +
 * `pipelineConfigId`，modes registry 单源解析）——**会话出生即定**（D1/D9）：
 * 出生解析一次入快照，发送链逐消息读快照、永不重解析。选择器选非默认模式 =
 * 经本通道开新管道会话并跳转（store 创建流随建即激活 = 已切到聊天区）；
 * 面板桥（mode.session）= 同通道 + 可选扩展位（执行者绑定/开演档/首条触发
 * 消息，模式包特有语义经扩展参数承载，宿主零模式知识）。
 *
 * 诚实降级：registry 不可达 → 会话照常创建，不带专属管道/主题/人设接管提示
 * （缺省共享 autonomous，与无声明同语义），不阻断。
 */

import { fetchModesRegistry, type ModeDeclaration } from '@/services/api/modes'
import { MODE_ROLEPLAY_ENDPOINTS } from '@/services/api/endpoints.generated'
import { getPresetTheme, validateThemeConfig } from '@/services/themeService'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { getModePanelTarget } from '@/services/schema/modePanel'
import { isModeIdShape, primaryModeTag } from '@/services/schema/modeOptions'
import {
  loadSessionExecutionOptions,
  saveSessionExecutionOptions,
} from '@/services/sessionExecutionOptions'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import {
  savePipelineBinding,
} from '@/services/pipelineExecutionOptions'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { openPluginPage } from '@/services/workspacePanelOpener'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionThemeStore } from '@/stores/sessionThemeStore'
import { generateUUID } from '@/utils/uuid'
import type { ThemeConfig } from '@/types/theme'

/** 出生扩展位：模式包/入口特有语义经此承载（宿主零模式知识） */
export interface ModeSessionBirthExtensions {
  /** 会话绑定执行者 agent 键（模式包 agents 命名空间，如 mode_X/<stem>）；
   *  缺省 = 主 agent 身份（发送链不带 agent_id） */
  agentId?: string
  /** 绑定显示名（输入区指示条展示用；缺省回退 agentId 尾段） */
  agentName?: string
  /** 会话标题覆盖（缺省 = registry 模式显示名 → mode 键 → 宿主默认标题） */
  title?: string
  /** 随会话快照落地的扩展 execution_context 键（开演档等；发送链逐消息并入） */
  extraContext?: Record<string, string>
  /** 创建后自动发送的首条触发消息（开演档；缺省不发送） */
  firstMessage?: string
  /** 开进既有会话（B8 管道标签形态）：不给 = 新会话出生（缺省）；给了 =
   *  向该会话开通子管道（mode_pipeline_open 代理 → 子 Tab + 管道级绑定），
   *  会话出生语义不动（既有会话 modeBinding 不被改写） */
  intoSessionId?: string
  /** 玩法级管道绑定显式覆盖（世界内嵌谱系右移批次一，调研 §6.4）：开演载荷
   *  携包上下文时透传包声明管道（清点条目 pipeline 键，服务端已过登记校验）；
   *  缺省 = mode 绑定管道（现役零变化）。值合法性执法在 bind 侧（插件端点
   *  登记闸 fail-closed 回落缺省 + 警示面），本通道只透传不复判 */
  pipelineConfigId?: string
  /** 卡主题档（完整 ThemeConfig，来自所选卡的 theme 字段）：出生即钉入该会话
   *  主题 override 栈——卡主题不随会话出生则新会话无 override，聊天回落全局
   *  主题（面板浏览期 applyCardTheme 钉的是**当时活跃**会话，跨会话不继承）。
   *  载荷非法在桥 parse 侧 fail-closed 丢该位；入栈位复用 pushTheme 二次校验 */
  cardTheme?: ThemeConfig
}

/** registry 按 mode 键取声明（fetchQuery 共享缓存）；reachable=false =
 *  registry 不可达（调用方降级不阻断）；decl=null 但 reachable=true = 模式未收录 */
async function lookupModeDeclaration(
  mode: string,
): Promise<{ decl: ModeDeclaration | null; reachable: boolean }> {
  if (!mode) return { decl: null, reachable: true }
  try {
    const registry = await queryClient.fetchQuery({
      queryKey: queryKeys.modesRegistry,
      queryFn: fetchModesRegistry,
      staleTime: 5 * 60_000,
    })
    return { decl: registry.modes.find((m) => m.mode === mode) ?? null, reachable: true }
  } catch {
    return { decl: null, reachable: false }
  }
}

/** 附身注入键解析结果（mode.possess 桥建立时消费，批 G② 通用化） */
export type PersonaKeyResolution =
  | { status: 'resolved'; personaKey: string }
  | { status: 'unreachable' }
  | { status: 'undeclared' }

/**
 * registry 解析附身注入键（decl.persona.from——注入键随声明，零硬编码）：
 * registry 可达且声明 persona.from → resolved；registry 可达但该模式未声明
 * persona（或未收录）→ undeclared（附身无承载，桥侧 fail-closed 拒绝）；
 * registry 不可达 → unreachable（附身可建立，发送链诚实降级不注入）。
 */
export async function resolvePersonaInjectionKey(mode: string): Promise<PersonaKeyResolution> {
  const { decl, reachable } = await lookupModeDeclaration(mode)
  if (!reachable) return { status: 'unreachable' }
  const from = decl?.persona?.from
  if (typeof from === 'string' && from.trim()) return { status: 'resolved', personaKey: from }
  return { status: 'undeclared' }
}

/**
 * registry 取出生默认模式声明（M1 并入刀：mode.yaml default:true 至多一个，
 * 后端 agent_manager 声明校验执法多标整批复决）。无 default 声明 / registry
 * 不可达 = null（诚实降级：出生不带绑定，与无声明同语义）。
 */
async function lookupDefaultModeDeclaration(): Promise<ModeDeclaration | null> {
  try {
    const registry = await queryClient.fetchQuery({
      queryKey: queryKeys.modesRegistry,
      queryFn: fetchModesRegistry,
      staleTime: 5 * 60_000,
    })
    return registry.modes.find((m) => m.default === true) ?? null
  } catch {
    return null
  }
}

/** mode.session 桥载荷（模式会话出生，面板侧携模式键 + 模式包特有扩展位） */
export interface ModeSessionPayload {
  /** 模式键（必填，形态守卫） */
  mode: string
  /** 会话绑定执行者 agent 键（模式包 agents 命名空间） */
  agentId?: string
  /** 绑定显示名（指示条；agentId 在场时必要） */
  agentName?: string
  /** 会话标题覆盖 */
  title?: string
  /** 随会话逐消息并入 execution_context 的扩展键 */
  extraContext?: Record<string, string>
  /** 创建后自动发送的首条触发消息 */
  firstMessage?: string
  /** 开进既有会话（B8）：'current' = 活跃会话；显式会话 id 原样透传 */
  intoSessionId?: string
  /** 玩法级管道绑定（wire 键 pipeline_config_id = 内核 chat.send_message 参数
   *  同名；插件端点 bind 侧登记闸执法，桥载荷只透传形态） */
  pipelineConfigId?: string
  /** 卡主题档（wire 键 card_theme = 卡库 /data/cards 条目的 theme 字段形态：
   *  完整 ThemeConfig）。非法档在 parse 侧 fail-closed 丢该位（出生零阻断） */
  cardTheme?: ThemeConfig
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined
}

/**
 * mode.session 载荷 → 出生扩展（fail-closed）：mode 形态守卫必填；agent_id/
 * name/title/first_message 在场须非空字符串（agent_id 在场须携 name——指示条
 * 显示名）；extra_context 在场须为键值均非空字符串的 plain object。非法返回
 * null（桥侧整包丢弃零状态变更并回 error）。
 * card_theme 为唯一「丢该位不丢整包」的扩展位：档形态非法时出生照常进行（会话
 * 创建是主流程，主题只是增补），缺位/丢弃都表现为键缺席=现役零变化。
 */
export function parseModeSessionPayload(payload: unknown): ModeSessionPayload | null {
  if (typeof payload !== 'object' || payload === null) return null
  const p = payload as Record<string, unknown>
  if (!isModeIdShape(p.mode)) return null
  const parsed: ModeSessionPayload = { mode: p.mode }
  for (const [key, field] of [
    ['agent_id', 'agentId'],
    ['name', 'agentName'],
    ['title', 'title'],
    ['first_message', 'firstMessage'],
    ['into_session_id', 'intoSessionId'],
    ['pipeline_config_id', 'pipelineConfigId'],
  ] as const) {
    const raw = p[key]
    if (raw === undefined) continue
    const value = nonEmptyString(raw)
    if (!value) return null
    parsed[field] = value
  }
  if (parsed.agentId && !parsed.agentName) return null
  if (p.card_theme !== undefined) {
    // 档形态校验单源（validateThemeConfig，与 pushTheme 入栈面同判）
    if (validateThemeConfig(p.card_theme).valid) parsed.cardTheme = p.card_theme as ThemeConfig
  }
  if (p.extra_context !== undefined) {
    if (typeof p.extra_context !== 'object' || p.extra_context === null) return null
    const extra: Record<string, string> = {}
    for (const [key, value] of Object.entries(p.extra_context)) {
      if (!key.trim()) return null
      const text = nonEmptyString(value)
      if (!text) return null
      extra[key] = text
    }
    parsed.extraContext = extra
  }
  return parsed
}

/**
 * 模式主题通道（批 G⑤）：decl.theme → 会话主题 override（sessionThemeStore
 * 会话栈，作用域=聊天区+面板容器——「会话内生效」按既有通道现状能力实现，
 * 不动全局 themeStore 持久档）。theme id 须解析为预设 ThemeConfig
 * （getPresetTheme——会话栈消费整档配置；插件/用户主题 id 无同步取档面，
 * 诚实降级零动作）；栈内已有同 id 档幂等不重压（面板 theme.apply 压的档
 * 不被顶掉）。返回是否压栈。
 */
export function ensureModeTheme(sessionId: string, themeId: string): boolean {
  const config = getPresetTheme(themeId)
  if (!config) return false
  const stack = useSessionThemeStore.getState().stacks[sessionId] ?? []
  if (stack.some((c) => c.id === config.id)) return false
  return useSessionThemeStore.getState().pushTheme(sessionId, config)
}

/**
 * 模式主题随会话激活（出生后/重载后的补同步）：活跃会话 modeBinding 的模式
 * 声明 theme 非空 → ensureModeTheme（幂等；无 mode/无声明/registry 不可达
 * 零动作）。异步面（registry fetchQuery 共享缓存），调用方 fire-and-forget。
 */
export async function syncModeThemeForSession(sessionId: string): Promise<void> {
  const mode = primaryModeTag(loadSessionExecutionOptions(sessionId)?.modeBinding?.mode)
  if (!mode) return
  const { decl } = await lookupModeDeclaration(mode)
  if (decl?.theme) ensureModeTheme(sessionId, decl.theme)
}

/**
 * 以某模式开新会话：建会话（store 编排激活/标签/主管道注册，随建即跳转）→
 * 该会话执行选项写入 modeBinding（出生解析一次）+ 扩展位（agentId 绑定/开演
 * 档快照，既有快照原样保留其余键，无快照落最小包）→ 主题出生钉（卡主题优先，
 * 缺省模式声明主题）→ 人设接管声明提示（D8）
 * → 面板页声明随开（getModePanelTarget 声明驱动）→ 可选首条触发消息。
 * mode 空串 = 选择器「默认」档出生：绑 registry default 声明模式（M1 并入刀；
 * 无 default/不可达 = 不带 modeBinding 的诚实降级）。回执 themePinned 如实报
 * 出生是否钉入主题（禁静默降级：不钉也说 false）。
 * 返回出生回执（sessionId + 落定的 modeBinding）。
 */
/**
 * B8 管道标签形态：向既有会话开通模式子管道（零内核——经 mode_pipeline_open
 * 插件端点代理内核 chat.send_message 创建分支）。子 Tab 新增并激活（主 Tab
 * 不动）；模式参数落**管道级绑定**（发送链优先序：管道绑定 > 会话快照——
 * 既有会话的出生语义不被改写）。首条消息经 WS 帧投给新管道（agent_id/
 * pipeline_config_id 帧参数与会话消息同口径）。
 */
async function openModePipelineIntoSession(
  mode: string,
  ext: ModeSessionBirthExtensions,
): Promise<{ sessionId: string; modeBinding?: { mode: string | string[]; pipelineConfigId?: string }; themePinned: boolean }> {
  const intoSessionId = ext.intoSessionId as string
  const { decl } = await lookupModeDeclaration(mode)
  // 端点职责就地（用户裁定）：模式插件自己的接口（roleplay 直调本插件端点；
  // 其他模式包复用 = 同款端点约定），不设独立代理插件。
  const apiClient = (await import('@/services/api/client')).apiClient
  const res = await apiClient.post<{
    pipeline_id: string
    thread_id: string
    pipeline_config_id: string
    pipeline_warning?: string
  }>(MODE_ROLEPLAY_ENDPOINTS.pipeline_open, {
    session_id: intoSessionId,
    mode,
    agent_id: ext.agentId,
    first_message: ext.firstMessage,
    // 玩法级管道绑定透传（批次一）：端点 bind 侧登记闸执法（未登记 fail-closed
    // 回落缺省），本通道只透传；缺省不带键 = 现役 roleplay 路径零变化
    ...(ext.pipelineConfigId ? { pipeline_config_id: ext.pipelineConfigId } : {}),
  })
  // bind 侧警示面（错误面可见）：端点回落缺省时通知可辨；派发本身不受阻
  if (res.data.pipeline_warning) {
    useNotificationStore.getState().addNotification({
      title: '玩法管道回落',
      message: res.data.pipeline_warning,
      priority: 'normal',
      category: 'alert',
      isBlocking: false,
      autoDismissMs: 8000,
      sourceLabel: '模式会话',
    })
  }
  const pipelineId = res.data.pipeline_id
  const binding = {
    mode,
    ...(res.data.pipeline_config_id ? { pipelineConfigId: res.data.pipeline_config_id } : {}),
    ...(ext.agentId ? { agentId: ext.agentId } : {}),
  }
  savePipelineBinding(pipelineId, binding)
  // 子 Tab 新增并激活（主 Tab 不动；管道级 agent 身份随 Tab 展示）
  useAgentTabStore.getState().openSubAgentTab({
    agentId: ext.agentId ?? `mode_${mode}`,
    agentName: ext.agentName ?? decl?.name ?? mode,
    parentRecordId: intoSessionId,
    pipelineId,
    setActive: true,
  })
  // 面板随开（与出生通道同语义：选择该模式 = 进入其工作面）
  const panelTarget = getModePanelTarget(mode)
  if (panelTarget) openPluginPage(panelTarget)
  // 首条触发消息投给新管道（帧参数走管道绑定同口径）
  if (ext.firstMessage?.trim()) {
    globalWS.sendUserInput(intoSessionId, ext.firstMessage, {
      pipelineId,
      clientMessageId: `b8_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      executionContext: { mode },
      agentId: ext.agentId,
      pipelineConfigId: binding.pipelineConfigId,
    })
  }
  if (decl?.persona?.replace) {
    useNotificationStore.getState().addNotification({
      title: `已进入「${decl.name}」模式`,
      message: '该模式会接管人设，当前会话上下文将重新载入。',
      // 会话坐标：模式提示按会话语义去重（同正文指纹闸不吞另一会话的提示），
      // 删会话时随 pruneForTarget 摘除
      sessionId: intoSessionId,
      priority: 'normal',
      category: 'info',
      isBlocking: false,
      autoDismissMs: 8000,
      sourceLabel: '模式会话',
    })
  }
  return {
    sessionId: intoSessionId,
    modeBinding: { mode, ...(binding.pipelineConfigId ? { pipelineConfigId: binding.pipelineConfigId } : {}) },
    // B8 开进既有会话 = 会话出生语义不动（override 栈属既有会话，补钉登记为
    // 后续批）：出生钉事实位如实回 false。
    themePinned: false,
  }
}

export async function openModeSession(
  mode: string,
  ext: ModeSessionBirthExtensions = {},
): Promise<{ sessionId: string; modeBinding?: { mode: string | string[]; pipelineConfigId?: string }; themePinned: boolean }> {
  if (ext.intoSessionId) {
    return openModePipelineIntoSession(mode, ext)
  }
  // 出生无显式选择（mode 空串 = 选择器「默认」档）→ 绑 default 声明模式（M1
  // 并入刀，替代"空串 = 不带键"）；无 default 声明 / registry 不可达 = 维持
  // 无绑定出生（诚实降级，与无声明同语义）。
  let effectiveMode = mode
  if (!mode) {
    effectiveMode = (await lookupDefaultModeDeclaration())?.mode ?? ''
  }
  const { decl } = await lookupModeDeclaration(effectiveMode)
  const conversationPipeline = decl?.pipelines.find((p) => p.context === 'conversation')?.name
  // 玩法级管道绑定显式覆盖优先（批次一）；缺省 = mode 绑定管道（现役零变化）
  const pipelineConfigId = ext.pipelineConfigId || conversationPipeline
  const modeBinding = effectiveMode
    ? { mode: [effectiveMode], ...(pipelineConfigId ? { pipelineConfigId } : {}) }
    : undefined
  const created = await useSessionListStore
    .getState()
    .createSession(
      ext.title ?? decl?.name ?? (effectiveMode || undefined),
      // X13-F1 服务端出生绑定面：执行者键（完整模式键 mode_X/<stem>，桥载荷
      // agent_id，与 WS 帧/B8 管道绑定同源同值）随创建请求落服务端——kernel
      // 出生绑定 fail-closed 自动接管（state agent.id + sessions 投影 +
      // thread→agent 注册表），换设备/清缓存后身份由服务端真值恢复（身份轴
      // 唯一真值=会话绑定 agent_id，模式体系设计 §4.2；弹窗轨 SessionEditModal
      // 同口径）。本地快照面保留（主题/执行选项/指示条继续用）；无绑定出生
      // 不伪造键（单参调用，与「无声明同语义」降级一致）。
      ...(ext.agentId ? [{ agentId: ext.agentId }] : []),
    )
  const snapshot = loadSessionExecutionOptions(created.id)
  saveSessionExecutionOptions(created.id, {
    values: snapshot?.values ?? {},
    executionContext: snapshot?.executionContext,
    ...(modeBinding ? { modeBinding } : {}),
    ...(ext.agentId
      ? { agentId: ext.agentId, ...(ext.agentName ? { agentName: ext.agentName } : {}) }
      : {}),
    ...(ext.extraContext ? { extraContext: ext.extraContext } : {}),
  })

  // D8 提示义务（会话侧）：模式声明 persona 接管（persona.replace）→ 建会话
  // 即告知缓存代价（人设占位符在前缀前部，换源破前缀缓存）；未声明不提示。
  if (decl?.persona?.replace) {
    useNotificationStore.getState().addNotification({
      title: `已进入${decl.name}会话`,
      message: '该模式会接管人设，将改变提示词前缀、破坏缓存命中（成本上升）。',
      // 会话坐标：模式提示按会话语义去重（同正文指纹闸不吞另一会话的提示），
      // 删会话时随 pruneForTarget 摘除
      sessionId: created.id,
      priority: 'normal',
      category: 'info',
      isBlocking: false,
      autoDismissMs: 8000,
      sourceLabel: '模式会话',
    })
  }

  // 面板页随开（声明驱动：contributes.pages 带 mode 键的面板页；无声明零动作。
  // 幂等：已开 = 激活）。新建会话管道 state.mode 尚未落定，等「进 tab」事件按
  // mode 触发会错过弹出窗口——语义明确（点名选择该模式 = 进入其工作面），直接
  // 开；页面 autoOpen:false 选退只约束自动通道（modePanelAutoOpen），显式点名
  // 不受限。默认档出生（mode 空串解析到 default 声明）= 用户未选择任何模式，
  // 与 Sidebar 新建会话路径同义——环境模式不抢焦点，不随开。
  if (mode) {
    const panelTarget = getModePanelTarget(effectiveMode)
    if (panelTarget) openPluginPage(panelTarget)
  }

  // 主题随出生：卡主题（卡库条目 theme 载荷，更具体）优先，缺省回落模式声明
  // 主题通道（批 G⑤ decl.theme）；两者皆缺席 = 零动作（现役回落全局主题）。
  // 入栈复用 pushTheme 既有 fail-closed 校验（档非法 → 零状态变更）。
  // 现状边界：override 栈不入持久化 → 重载后卡主题不自动补（syncModeThemeForSession
  // 只按 modeBinding 补模式声明主题，卡主题的补同步面属后续批登记项）。
  const themePinned = ext.cardTheme
    ? useSessionThemeStore.getState().pushTheme(created.id, ext.cardTheme)
    : decl?.theme
      ? ensureModeTheme(created.id, decl.theme)
      : false

  // 首条触发消息（开演档）：execution_context 与发送链同一并入形态（mode 键 +
  // 扩展上下文），agentId/pipelineConfigId 走帧参数——首条与后续消息注入口径一致。
  if (ext.firstMessage?.trim()) {
    globalWS.sendUserInput(created.id, ext.firstMessage, {
      pipelineId: created.pipelineIds?.[0],
      clientMessageId: generateUUID(),
      executionContext: {
        ...(effectiveMode ? { mode: [effectiveMode] } : {}),
        ...(ext.extraContext ?? {}),
      },
      ...(ext.agentId ? { agentId: ext.agentId } : {}),
      ...(modeBinding?.pipelineConfigId ? { pipelineConfigId: modeBinding.pipelineConfigId } : {}),
    })
  }
  return { sessionId: created.id, ...(modeBinding ? { modeBinding } : {}), themePinned }
}

/** 会话执行选项里的执行者绑定读出形态（name 缺席回退 agentId 尾段） */
export interface SessionAgentBinding {
  agentId: string
  name: string
}

/** 读会话的执行者绑定；无绑定返回 null（指示条不渲染） */
export function readSessionAgentBinding(threadId: string): SessionAgentBinding | null {
  const snapshot = loadSessionExecutionOptions(threadId)
  if (!snapshot?.agentId) return null
  const slash = snapshot.agentId.indexOf('/')
  const tail = slash >= 0 ? snapshot.agentId.slice(slash + 1) : snapshot.agentId
  return { agentId: snapshot.agentId, name: snapshot.agentName || tail }
}

/** 退出绑定会话：清该会话执行选项的绑定键，其余快照键原样保留 */
export function clearSessionAgentBinding(threadId: string): void {
  const snapshot = loadSessionExecutionOptions(threadId)
  if (!snapshot) return
  saveSessionExecutionOptions(threadId, { ...snapshot, agentId: undefined, agentName: undefined })
}

/**
 * 出生无显式选择的默认绑定补挂（M1 并入刀）：Sidebar「新建会话」等不经
 * openModeSession 的出生通道在落会话后调用（fire-and-forget）——registry 有
 * default 声明 → 补写 modeBinding（出生即定语义与选择器出生一致）；已有绑定
 * （显式选择/重复调用）不覆盖，无 default 声明 / registry 不可达零动作。
 * 自含错误面：本地快照写失败 = 会话无绑定（与无声明同语义）降级不上抛
 * （fire-and-forget 调用方无接收面，绑定是增补不是会话创建主体）。
 */
export async function applyDefaultModeBinding(sessionId: string): Promise<void> {
  try {
    const snapshot = loadSessionExecutionOptions(sessionId)
    if (snapshot?.modeBinding) return
    const decl = await lookupDefaultModeDeclaration()
    if (!decl) return
    const pipeline = decl.pipelines.find((p) => p.context === 'conversation')?.name
    saveSessionExecutionOptions(sessionId, {
      values: snapshot?.values ?? {},
      executionContext: snapshot?.executionContext,
      modeBinding: {
        mode: [decl.mode],
        ...(pipeline ? { pipelineConfigId: pipeline } : {}),
      },
    })
  } catch {
    // 降级即处理：无绑定出生（诚实语义），不向 fire-and-forget 调用方传播
  }
}
