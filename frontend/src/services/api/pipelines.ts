/** 管道运行快照 API 服务（统一管道管理数据源，内核 `/api/v1/pipelines/runs`） */

import { API_ENDPOINTS } from '@/constants/api'
import { apiClient } from '@/services/api/client'
import type { PipelineRunInfo, PipelineStatus } from '@/types/pipeline'

/** 管道运行快照响应 */
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

/** 管道 state 摘要（内核白名单裁剪：phase/迭代/上下文，messages 只出口条数） */
export interface PipelineStateSummary {
  current_phase?: string
  status?: string
  ended?: boolean
  session_id?: string
  pipeline_id?: string
  agent_id?: string
  config_id?: string
  display_name?: string
  name?: string
  message_count?: number
  ckpt_max_seq?: number
  max_iterations?: number
  metadata?: Record<string, unknown>
  raw_error?: string | null
  // GAP-1 任务域/血缘字段（内核 STATE_SUMMARY_KEYS 以扁平点号键出口）
  'task.goal'?: string
  'task.status'?: string
  'task.id'?: string
  'task.ended_at'?: string
  'lineage.parent_pipeline_id'?: string
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
  /** input/core/output（plugin.json pipeline_role，仅元数据标签） */
  role: string | null
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
  /** input/core/output；目录缺失时 null */
  role: string | null
  hostType: string
  version: string | null
  /** 三态：true/false=状态接口返回；null=状态侧缺失（禁用后下架/分页遗漏）——不默认已启用 */
  enabled: boolean | null
  configFiles: Array<{ id: string; label: string; path: string }>
}

/**
 * 拉取管道插件目录：并行 GET /api/v1/pipelines（role）+ GET /api/v1/plugins
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
      role: catalog?.role ?? null,
      hostType: catalog?.host_type ?? status?.host_type ?? '',
      version: catalog?.version ?? status?.version ?? null,
      // 状态侧缺失用 null（三态"未知"），对齐同函数 role 用 null 的标准——
      // 默认 enabled:true 会让"禁用后下架"的插件在编辑器显示"已启用"（FE7）
      enabled: status?.enabled ?? null,
      configFiles: status?.config_files ?? [],
    })
  }
  entries.sort((a, b) => a.id.localeCompare(b.id))
  return entries
}
