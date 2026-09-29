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
import { getPresetTheme } from '@/services/themeService'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { getModePanelTarget } from '@/services/schema/modePanel'
import { isModeIdShape } from '@/services/schema/modeOptions'
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
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined
}

/**
 * mode.session 载荷 → 出生扩展（fail-closed）：mode 形态守卫必填；agent_id/
 * name/title/first_message 在场须非空字符串（agent_id 在场须携 name——指示条
 * 显示名）；extra_context 在场须为键值均非空字符串的 plain object。非法返回
 * null（桥侧整包丢弃零状态变更并回 error）。
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
  ] as const) {
    const raw = p[key]
    if (raw === undefined) continue
    const value = nonEmptyString(raw)
    if (!value) return null
    parsed[field] = value
  }
  if (parsed.agentId && !parsed.agentName) return null
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
  const mode = loadSessionExecutionOptions(sessionId)?.modeBinding?.mode
  if (!mode) return
  const { decl } = await lookupModeDeclaration(mode)
  if (decl?.theme) ensureModeTheme(sessionId, decl.theme)
}

/**
 * 以某模式开新会话：建会话（store 编排激活/标签/主管道注册，随建即跳转）→
 * 该会话执行选项写入 modeBinding（出生解析一次）+ 扩展位（agentId 绑定/开演
 * 档快照，既有快照原样保留其余键，无快照落最小包）→ 人设接管声明提示（D8）
 * → 面板页声明随开（getModePanelTarget 声明驱动）→ 可选首条触发消息。
 * mode 空串 = 默认会话出生（不带 modeBinding，选择器「默认」档的出生形态）。
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
): Promise<{ sessionId: string; modeBinding?: { mode: string; pipelineConfigId?: string } }> {
  const intoSessionId = ext.intoSessionId as string
  const { decl } = await lookupModeDeclaration(mode)
  // 端点职责就地（用户裁定）：模式插件自己的接口（roleplay 直调本插件端点；
  // 其他模式包复用 = 同款端点约定），不设独立代理插件。
  const apiClient = (await import('@/services/api/client')).apiClient
  const res = await apiClient.post<{
    pipeline_id: string
    thread_id: string
    pipeline_config_id: string
  }>('/ext/mode_roleplay/pipeline/open', {
    session_id: intoSessionId,
    mode,
    agent_id: ext.agentId,
    first_message: ext.firstMessage,
  })
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
      title: `已开进${decl.name}管道`,
      message: '该模式会接管人设，将改变提示词前缀、破坏缓存命中（成本上升）。',
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
  }
}

export async function openModeSession(
  mode: string,
  ext: ModeSessionBirthExtensions = {},
): Promise<{ sessionId: string; modeBinding?: { mode: string; pipelineConfigId?: string } }> {
  if (ext.intoSessionId) {
    return openModePipelineIntoSession(mode, ext)
  }
  const { decl } = await lookupModeDeclaration(mode)
  const conversationPipeline = decl?.pipelines.find((p) => p.context === 'conversation')?.name
  const modeBinding = mode
    ? { mode, ...(conversationPipeline ? { pipelineConfigId: conversationPipeline } : {}) }
    : undefined
  const created = await useSessionListStore
    .getState()
    .createSession(ext.title ?? decl?.name ?? (mode || undefined))
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
      priority: 'normal',
      category: 'info',
      isBlocking: false,
      autoDismissMs: 8000,
      sourceLabel: '模式会话',
    })
  }

  // 面板页随开（声明驱动：contributes.pages 带 mode 键的面板页；无声明零动作。
  // 幂等：已开 = 激活）。新建会话管道 state.mode 尚未落定，等「进 tab」事件按
  // mode 触发会错过弹出窗口——语义明确（选择该模式 = 进入其工作面），直接开。
  const panelTarget = getModePanelTarget(mode)
  if (panelTarget) openPluginPage(panelTarget)

  // 模式主题随出生（批 G⑤）：decl.theme 非空 → 会话主题 override 入栈（无
  // 声明/不可解析零动作——诚实降级不发明）；后续激活由
  // syncModeThemeForSession 幂等补同步（重载后会话栈丢失场景）。
  if (decl?.theme) ensureModeTheme(created.id, decl.theme)

  // 首条触发消息（开演档）：execution_context 与发送链同一并入形态（mode 键 +
  // 扩展上下文），agentId/pipelineConfigId 走帧参数——首条与后续消息注入口径一致。
  if (ext.firstMessage?.trim()) {
    globalWS.sendUserInput(created.id, ext.firstMessage, {
      pipelineId: created.pipelineIds?.[0],
      clientMessageId: generateUUID(),
      executionContext: {
        ...(mode ? { mode } : {}),
        ...(ext.extraContext ?? {}),
      },
      ...(ext.agentId ? { agentId: ext.agentId } : {}),
      ...(modeBinding?.pipelineConfigId ? { pipelineConfigId: modeBinding.pipelineConfigId } : {}),
    })
  }
  return { sessionId: created.id, ...(modeBinding ? { modeBinding } : {}) }
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
