/** 管道运行快照 API 服务（统一管道管理数据源，内核 `/api/v1/pipelines/runs`） */

import { API_ENDPOINTS } from '@/constants/api'
import { apiClient } from '@/services/api/client'
import { normalizeModeTags } from '@/services/schema/modeOptions'
import type { PipelineRunInfo, PipelineStatus } from '@/types/pipeline'

export interface PipelineRunsResponse {
  items: PipelineRunInfo[]
}

/** 获取管道运行快照（按开始时间倒序；status 可选过滤） */
export async function fetchPipelineRuns(params?: {
  status?: PipelineStatus
  limit?: number
}): Promise<PipelineRunInfo[]> {
  const query = new URLSearchParams()
  if (params?.status) {
    query.append('status', params.status)
  }
  if (params?.limit) {
    query.append('limit', String(params.limit))
  }
  const qs = query.toString()
  const response = await apiClient.get<PipelineRunsResponse>(
    `${API_ENDPOINTS.PIPELINES.RUNS}${qs ? `?${qs}` : ''}`,
  )
  return response.data.items ?? []
}

/**
 * state["track.llm_usage"] 结构（track Output 插件跨轮累加，plugin.py
 * _collect_token_usage）。`total_*` 是管道累计，`last_*` 是本轮单轮值。
 * 输入框指示器用 last_* 表达「当前上下文窗口占用」。
 */
export interface PipelineLlmUsage {
  total_input_tokens?: number
  total_output_tokens?: number
  total_tokens?: number
  total_cached_tokens?: number
  total_missed_tokens?: number
  total_cache_hit_ratio?: number
  last_input_tokens?: number
  last_output_tokens?: number
  last_cached_tokens?: number
  last_missed_tokens?: number
  last_cache_hit_ratio?: number
}

/** 管道 state 摘要（内核白名单裁剪：phase/迭代/上下文，messages 只出口条数） */
export interface PipelineStateSummary {
  current_phase?: string
  status?: string
  ended?: boolean
  /** 实际状态（内核持有，RunStatus 五态小写；插件不可写，可观测性唯一运行状态真值） */
  run_status?: 'running' | 'suspended' | 'completed' | 'failed' | 'cancelled'
  /** 终局署名（run 收尾落表；空串 = 未署名，如 cancelled/正常收束） */
  run_stop_reason?: string
  /** 挂起归因（run 域，drain 排空写面 ADR 2026-11-04-run-suspended-reason-key；
   *  空串 = 未署名，恢复即清） */
  suspended_reason?: string
  session_id?: string
  pipeline_id?: string
  agent_id?: string
  config_id?: string
  /** 执行身份（state 持久键 agent.id，tasks 插件 export_fields 声明出口；
   *  会话主管道由 session router overlay 写入、任务管道由出生协议透传） */
  'agent.id'?: string
  message_count?: number
  ckpt_max_seq?: number
  max_iterations?: number
  metadata?: Record<string, unknown>
  raw_error?: string | null
  /** 管道标题（内核 state.title；出生/改名由启用方或改名腿刷新，空 = 走短 ID 兜底） */
  title?: string
  /** 插件级失败常驻镜像（内核基线键出口）：[{plugin_id, code, message}]，
   * 失败归因的全量真值（raw_error 引擎投影键缺席时的兜底数据源） */
  'run.plugin_errors'?: Array<{
    plugin_id?: string
    code?: string
    message?: string
  }>
  // GAP-1 任务域/血缘字段（内核 STATE_SUMMARY_KEYS 以扁平点号键出口）
  'task.goal'?: string
  'task.status'?: string
  'task.id'?: string
  'task.ended_at'?: string
  'task.ws_meta'?: { path?: string }
  /** 任务域失败分类（tasks 插件失败终态随写；非空 = 有归因分类） */
  'task.failure_class'?: string
  /** 任务域失败归因全文（tasks 插件已译人话文本；空/缺 = 无归因） */
  'task.failure_reason'?: string
  // 任务模式键（withTaskMode/withModeBinding 并入 execution_context，内核合并
  // 后 state 落键）：**标签集**（字符串数组，单标签=长度一特例，空集=默认兜底；
  // string=旧形态，读取面 normalizeModeTags 归一）。自动会话不带键——管道视图
  // 模式徽标按此键的有无决定渲染
  mode?: string | string[]
  'lineage.parent_pipeline_id'?: string
  // 血缘根会话（task_submit 出生写面）：自环子任务管道（thread=自身 id）的
  // 真实归属用户会话，任务管理面板跨会话跳转的定位锚点
  'lineage.origin_session_id'?: string
  // LLM 观测字段（内核 STATE_SUMMARY_KEYS 放行）：track 插件每轮把累计/单轮
  // token 写入 state，llm_core 写入实际模型名——chat-input 空间 context_usage
  // 声明的数据源，覆盖输入框用量指示器（模型名 + 上下文圆环）。
  'track.llm_usage'?: PipelineLlmUsage
  'track.total_tokens'?: number
  'cost_control.total_tokens'?: number
  'cost_control.usage_percent'?: number
  'llm_model'?: string
  /** 管道上下文窗口上限（内核 state 出口，输入框用量指示器圆环的分母真值） */
  context_window?: number
  /** 压缩终态留痕（context_window_guard 出口）：失败时前端常驻提示「压缩失败」，
   *  避免只靠 8 秒 toast（压缩失败 = 上下文持续膨胀的静默劣化） */
  'context.compression'?: {
    status?: string
    reason?: string
    original_tokens?: number
    compressed_tokens?: number
    ended_at?: string
  }
  // 工作区坐标（workspace_lifecycle init 写入；R3 裁定：所有管道类型的工作区
  // 关联底座——path=worktree 副本或 plain 目录，project_root 是源根不用于关联）
  workspace?: string
  ws_meta?: { path?: string; project_root?: string; mode?: string }
}

/** GET /api/v1/pipelines/state 条目（会话/任务/迭代的运行时真值，任务树数据源） */
export interface PipelineStateInfo {
  pipeline_id: string
  thread_id?: string
  agent_id?: string | null
  /** memory=内存常驻（当前活跃）/ checkpoint=DB 冷数据兜底 */
  source: 'memory' | 'checkpoint'
  state: PipelineStateSummary
}

// ── state 摘要视图模型适配层（解耦方案 P2-1，仿 mapThreadToSession 样板）──
//
// 内核的扁平点号键（task.status / lineage.origin_session_id / track.llm_usage）
// 与 run_status/ended/raw_error 三源状态推断只允许出现在本文件；组件层一律
// 消费下方视图模型——插件 export_fields 键名改动只需改这里的映射单点。

/** 管道 state 摘要视图模型（组件层唯一消费面，不含任何内核原始键名） */
export interface PipelineStateViewModel {
  /** 归一运行状态（单一状态出口：run_status 直映，缺省回退三源推断） */
  status: PipelineStatus
  /** 循环体阶段（init/main/exit…，多循环体真值） */
  currentPhase?: string
  /** 消息条数（迭代轮次的粗粒度指标） */
  messageCount?: number
  /** 任务域状态原值（evaluating/planning 等细态不被运行态吞掉） */
  taskStatus?: string
  /** 任务模式标签集（state.mode 归一出口；管道视图模式徽标取数源，空集零渲染） */
  modeTags?: string[]
  /** 血缘根会话：自环子任务管道（thread=自身 id）的真实归属用户会话 */
  originSessionId?: string
  /** 工作区坐标（任务域镜像 task.ws_meta 优先，防会话工作区投影污染） */
  workspacePath?: string
  /** 实际运行模型名（llm_core 每轮写入） */
  llmModel?: string
  /** 管道上下文窗口上限（用量指示器圆环分母真值） */
  contextWindow?: number
  /** 压缩终态（applied/failed + 原因）；failed 时用量指示器常驻提示 */
  compressionStatus?: { status: string; reason?: string }
  /** LLM token 用量（track 插件跨轮累加） */
  llmUsage?: PipelineLlmUsage
  /** state 是否已结束（详情面展示用；状态推断走 status 单一出口） */
  ended?: boolean
  /** state 原始错误（详情面展示用） */
  rawError?: string
  /** 终局署名（详情面展示用；缺省 = 未署名，不渲染停止原因行） */
  stopReason?: string
  /** 挂起归因（详情面展示用；缺省 = 无归因，不渲染挂起原因行——P-23 片2） */
  suspendedReason?: string
  /** 任务域失败分类（state task.failure_class；缺省不渲染归因行） */
  failureClass?: string
  /** 任务域失败归因全文（state task.failure_reason；后端已译文本原样透传） */
  failureReason?: string
  /** 统一管道标题（state.title，ADR 2026-10-27-pipeline-title-field；
   *  启用方出生写入，无回退腿） */
  title?: string
  /** 执行身份 agent.id 原值（state 持久键；会话/任务管道同源，行级 agent
   *  名徽标的解析输入。端点顶层 agent_id 是派发路由坐标且冷行恒 null，
   *  不作执行身份） */
  agentId?: string
}

/** 管道 state 条目视图模型（摘要视图 + 条目级坐标 pipeline_id/thread_id） */
export interface PipelineStateEntryViewModel extends PipelineStateViewModel {
  pipelineId: string
  threadId?: string
}

/**
 * 归一运行状态（单一状态出口）：内核 run_status 五态直映（轮中=running 不失
 * 真）；ended/raw_error 保留终局推断（错误优先于结束）。三源全缺或词汇未知
 * = 获取不到运行状态真值 → unknown，不猜 running（用户裁定 2026-11-02「前端
 * 状态显示与后端数据完全一致，获取不到落 unknown」）。旧推断兜底已实证为
 * 幽灵执行中根源：从未起跑的任务管道出生行（内核 merge_cold_state 表行出口，
 * 仅 task.* 出生键）被猜成 running 显「运行中」，实际 pending 待执行——装机
 * 活体 2026-11-01（179d1e311bce/ffb5a77d5496）。
 */
export function resolvePipelineRunStatus(summary: PipelineStateSummary): PipelineStatus {
  const rs = summary.run_status
  if (rs === 'running' || rs === 'suspended' || rs === 'completed' || rs === 'failed' || rs === 'cancelled') {
    return rs
  }
  if (summary.raw_error) return 'failed'
  if (summary.ended === true) return 'completed'
  return 'unknown'
}

/** 非空串 → 原值，空串/缺失 → undefined（不伪造空值语义） */
function nonEmpty(value: string | null | undefined): string | undefined {
  return typeof value === 'string' && value !== '' ? value : undefined
}

/**
 * 插件级失败镜像 → 错误详情文本（全量透传，ADR 2026-10-27-pipeline-error-
 * notification-channel）：多条错误逐行拼接，空文本条目跳过，不截断——面板
 * 「错误」行是失败归因的全量读面，任何截断都会丢归因尾巴。
 */
function pluginErrorsText(
  errors: PipelineStateSummary['run.plugin_errors'],
): string | undefined {
  if (!Array.isArray(errors)) return undefined
  const messages = errors
    .map((e) => (typeof e?.message === 'string' ? e.message.trim() : ''))
    .filter(Boolean)
  return messages.length > 0 ? messages.join('\n') : undefined
}

/** 压缩终态映射：只收合法 status（applied/failed），其余忽略（不展示假状态） */
function mapCompressionStatus(
  raw?: { status?: string; reason?: string } | null,
): { status: string; reason?: string } | undefined {
  const status = raw?.status
  if (status !== 'applied' && status !== 'failed') return undefined
  return { status, reason: raw?.reason || undefined }
}

/** state 摘要（内核扁平键）→ 视图模型（键名映射单点） */
export function mapStateSummaryToViewModel(summary: PipelineStateSummary): PipelineStateViewModel {
  const wsPath =
    summary['task.ws_meta']?.path ?? summary.ws_meta?.path ?? summary.workspace
  return {
    status: resolvePipelineRunStatus(summary),
    currentPhase: summary.current_phase,
    messageCount: summary.message_count,
    taskStatus: nonEmpty(summary['task.status']),
    modeTags: normalizeModeTags(summary.mode),
    originSessionId: nonEmpty(summary['lineage.origin_session_id']),
    workspacePath: nonEmpty(wsPath),
    llmModel: nonEmpty(summary.llm_model),
    contextWindow: typeof summary.context_window === 'number' ? summary.context_window : undefined,
    compressionStatus: mapCompressionStatus(summary['context.compression']),
    llmUsage: summary['track.llm_usage'],
    ended: summary.ended === true ? true : undefined,
    // 失败归因：raw_error（引擎投影键）缺席时回退 run.plugin_errors 全量拼接
    // ——折叠失败轮（步失败零产出）raw_error 恒缺席、run_stop_reason 恒空串，
    // 此前详情面只剩 failed 无归因（2026-10-27 装机 1302 限流轮实证）。
    rawError: summary.raw_error ?? pluginErrorsText(summary['run.plugin_errors']),
    stopReason: nonEmpty(summary.run_stop_reason),
    // 挂起归因（run 域 drain 排空写面；空串 = 未署名，缺省不渲染归因行）
    suspendedReason: nonEmpty(summary.suspended_reason),
    // 任务域失败归因（tasks 插件失败终态随写键；缺省 = 无归因，回退现状只
    // 渲染停止原因行——字段不在时不伪造归因）
    failureClass: nonEmpty(summary['task.failure_class']),
    failureReason: nonEmpty(summary['task.failure_reason']),
    title: nonEmpty(summary.title),
    agentId: nonEmpty(summary['agent.id']),
  }
}

/** state 条目（含 pipeline_id/thread_id 坐标）→ 条目视图模型 */
export function mapStateInfoToViewModel(info: PipelineStateInfo): PipelineStateEntryViewModel {
  return {
    pipelineId: info.pipeline_id,
    threadId: info.thread_id,
    ...mapStateSummaryToViewModel(info.state),
  }
}

/** 管道 state 摘要列表响应 */
interface PipelineStatesResponse {
  items: PipelineStateInfo[]
}

/** 拉取管道 state 摘要（内核 PipelineStateRegistry + checkpoint 兜底） */
export async function fetchPipelineStates(): Promise<PipelineStateInfo[]> {
  const response = await apiClient.get<PipelineStatesResponse>(
    API_ENDPOINTS.PIPELINES.STATE,
  )
  return response.data.items ?? []
}

/** GET /api/v1/pipelines 条目（管道插件清单，pipelines_handler） */
interface PipelineCatalogItem {
  id: string
  name: string
  version: string | null
  host_type: string
}

/** GET /api/v1/plugins 条目（插件状态，plugins_status_handler，字段对齐后端） */
interface PluginStatusItem {
  plugin_id: string
  name: string
  config_type: string
  host_type: string
  version: string | null
  enabled: boolean
  config_files: Array<{ id: string; label: string; path: string }>
}

/** 管道可视化编辑器的插件目录条目（两接口按 id join） */
export interface PipelinePluginCatalogEntry {
  /** 插件 manifest id（autonomous.yaml steps 引用的名字，带 pipeline_ 前缀） */
  id: string
  name: string
  hostType: string
  version: string | null
  /** 三态：true/false=状态接口返回；null=状态侧缺失（禁用后下架/分页遗漏）——不默认已启用 */
  enabled: boolean | null
  configFiles: Array<{ id: string; label: string; path: string }>
}

/**
 * 拉取管道插件目录：并行 GET /api/v1/pipelines + GET /api/v1/plugins
 * （enabled/config_files），按 id 取并集 join 成统一条目。
 *
 * 任一接口失败即抛错（调用方降级为"目录不可用"，不阻塞配置编辑）。
 */
export async function fetchPipelinePluginCatalog(): Promise<PipelinePluginCatalogEntry[]> {
  const [catalogRes, pluginsRes] = await Promise.all([
    apiClient.get<PipelineCatalogItem[]>(API_ENDPOINTS.PIPELINES.CATALOG),
    apiClient.get<PluginStatusItem[]>(API_ENDPOINTS.PLUGINS.LIST),
  ])
  const catalogById = new Map(catalogRes.data.map((item) => [item.id, item]))
  const statusById = new Map(pluginsRes.data.map((item) => [item.plugin_id, item]))

  const ids = new Set<string>([...catalogById.keys(), ...statusById.keys()])
  const entries: PipelinePluginCatalogEntry[] = []
  for (const id of ids) {
    // 仅保留 pipeline 类插件（status 侧 config_type 过滤；catalog 侧本就只含 pipeline）
    const status = statusById.get(id)
    if (status && status.config_type !== 'pipeline') continue
    const catalog = catalogById.get(id)
    entries.push({
      id,
      name: catalog?.name ?? status?.name ?? id,
      hostType: catalog?.host_type ?? status?.host_type ?? '',
      version: catalog?.version ?? status?.version ?? null,
      // 状态侧缺失用 null（三态"未知"）——
      // 默认 enabled:true 会让"禁用后下架"的插件在编辑器显示"已启用"（FE7）
      enabled: status?.enabled ?? null,
      configFiles: status?.config_files ?? [],
    })
  }
  entries.sort((a, b) => a.id.localeCompare(b.id))
  return entries
}

// ── pending 输入队列（ADR-2026-08-26）──────────────────────────────

/** pending 输入来源标注（与内核 PendingInputSource 同枚举） */
export type PendingInputSource = 'user' | 'trigger' | 'task' | 'http' | 'system'

/** pending 输入条目（等待窗口内可修改/删除；激活后进主消息流） */
export interface PendingInputItem {
  id: string
  pipeline_id: string
  content: string
  source: PendingInputSource
  /** 客户端消息幂等键（用户来源条目非空）：returned 事件按它撤下对应
   *  queued 乐观气泡；触发器/任务来源条目为空串。 */
  client_message_id?: string
  created_at: string
}

/** GET /api/v1/pipelines/{id}/pending-inputs 响应 */
export interface PendingInputsResponse {
  items: PendingInputItem[]
}

/** 拉取某管道的待处理输入队列（FIFO 序） */
export async function fetchPendingInputs(pipelineId: string): Promise<PendingInputItem[]> {
  const response = await apiClient.get<PendingInputsResponse>(
    API_ENDPOINTS.PIPELINES.PENDING_INPUTS(pipelineId),
  )
  return response.data.items ?? []
}

/** 修改 pending 输入 content（等待窗口内生效；消费时从表取最新参数） */
export async function updatePendingInput(
  pipelineId: string,
  inputId: string,
  content: string,
): Promise<void> {
  await apiClient.put(
    API_ENDPOINTS.PIPELINES.PENDING_INPUT(pipelineId, inputId),
    { content },
  )
}

/** 删除单条 pending 输入 */
export async function deletePendingInput(pipelineId: string, inputId: string): Promise<void> {
  await apiClient.delete(API_ENDPOINTS.PIPELINES.PENDING_INPUT(pipelineId, inputId))
}

/** 清空某管道全部 pending 输入 */
export async function clearPendingInputs(pipelineId: string): Promise<void> {
  await apiClient.delete(API_ENDPOINTS.PIPELINES.PENDING_INPUTS(pipelineId))
}
