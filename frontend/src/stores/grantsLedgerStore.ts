/**
 * grants 账本管理页状态（P3 运营面）
 *
 * 页面级 Zustand store：grant 列表 + 筛选 + 撤销、审计流、四指标卡、
 * proto-grant（永久白名单/管道级免审批）只读面。数据全部经生成端点层
 * （endpoints.generated.ts）调用插件 http_endpoints，零手写 /ext/ 字面量。
 *
 * 设计真值：ADR 2026-11-19-grants-ledger-and-tool-classification +
 * docs/working/grants账本执行方案_20261119.md §5/§9。
 */

import { create } from 'zustand'
import apiClient from '@/services/api/client'
import {
  GRANTS_ENDPOINTS,
  PIPELINE_SECURITY_CHECK_ENDPOINTS,
} from '@/services/api/endpoints.generated'

/** grants 表行形态（GET /ext/grants/list 返回，与 ledger._row_to_grant 同构） */
export interface GrantRecord {
  grant_id: string
  subject_kind: string
  subject_id: string
  tool: string
  action: string
  scope: Record<string, unknown>
  granularity: string
  status: string
  max_uses: number | null
  uses: number
  created_at: string | null
  expires_at: string | null
  source: string
  evidence: string | null
  pipeline_id: string | null
  parent_task_id: string | null
}

/** 单指标条目（GET /ext/grants/metrics，metrics.py 契约） */
export interface MetricEntry {
  label: string
  caliber: string
  unit: string
  value: number | null
  degraded: boolean
  reason: string | null
  components: Record<string, number | null>
}

export interface MetricsResponse {
  metrics: Record<string, MetricEntry>
  sources: { security_audit_jsonl: { reachable: boolean; reason: string | null } }
}

/** 审计事件（GET /ext/grants/audit 返回，与 ledger.events 同构） */
export interface AuditEvent {
  event_id: string
  grant_id: string | null
  event_type: string
  ts: string
  context: Record<string, unknown> | null
}

/** 四指标闭集键（metrics.py METRIC_KEYS 投影） */
export const GRANT_METRIC_KEYS = [
  'approval_card_popup_rate',
  'grant_hit_rate',
  'revoke_to_effect',
  'uncategorized_blocked',
] as const

export type GrantMetricKey = (typeof GRANT_METRIC_KEYS)[number]

/** grant 状态闭集（ledger.STATUSES 投影） */
export const GRANT_STATUSES = ['active', 'consumed', 'revoked', 'expired'] as const

/** 主体类型闭集（ledger.SUBJECT_KINDS 投影） */
export const GRANT_SUBJECT_KINDS = ['user', 'agent', 'task', 'pipeline'] as const

/** 审计事件类型闭集（ledger.EVENT_TYPES 投影） */
export const GRANT_EVENT_TYPES = ['issue', 'consume', 'deny', 'revoke', 'expire'] as const

/** 列表筛选（subject/tool/status + 主体类型；后端 list 端点原生支持的查询面） */
export interface GrantFilters {
  subject_kind: string
  subject_id: string
  tool: string
  status: string
}

const EMPTY_FILTERS: GrantFilters = { subject_kind: '', subject_id: '', tool: '', status: '' }

/** 审计流展示条数上限（端点全量返回，前端截尾防长列表） */
const AUDIT_DISPLAY_LIMIT = 50

interface GrantsLedgerState {
  grants: GrantRecord[]
  filters: GrantFilters
  grantsLoading: boolean
  grantsError: string | null
  metrics: MetricsResponse | null
  metricsError: string | null
  auditEvents: AuditEvent[]
  auditTypeFilter: string
  auditLoading: boolean
  whitelist: string[]
  pipelineModes: Record<string, string>
  protoError: string | null
  revokingId: string | null
  setFilter: (patch: Partial<GrantFilters>) => void
  resetFilters: () => void
  setAuditTypeFilter: (value: string) => void
  loadGrants: () => Promise<void>
  loadMetrics: () => Promise<void>
  loadAudit: () => Promise<void>
  loadProtoGrants: () => Promise<void>
  loadAll: () => Promise<void>
  revokeGrant: (grantId: string) => Promise<boolean>
  removeWhitelistEntry: (signature: string) => Promise<boolean>
  revokePipelineMode: (pipelineId: string) => Promise<boolean>
}

/** 非空筛选 → query 参数（端点按参数过滤，空串 = 不过滤） */
function filtersToParams(filters: GrantFilters): Record<string, string> {
  const params: Record<string, string> = {}
  if (filters.subject_kind) params.subject_kind = filters.subject_kind
  if (filters.subject_id) params.subject_id = filters.subject_id
  if (filters.tool) params.tool = filters.tool
  if (filters.status) params.status = filters.status
  return params
}

export const useGrantsLedgerStore = create<GrantsLedgerState>((set, get) => ({
  grants: [],
  filters: { ...EMPTY_FILTERS },
  grantsLoading: false,
  grantsError: null,
  metrics: null,
  metricsError: null,
  auditEvents: [],
  auditTypeFilter: '',
  auditLoading: false,
  whitelist: [],
  pipelineModes: {},
  protoError: null,
  revokingId: null,

  setFilter: (patch) => set((s) => ({ filters: { ...s.filters, ...patch } })),
  resetFilters: () => set({ filters: { ...EMPTY_FILTERS } }),
  setAuditTypeFilter: (value) => set({ auditTypeFilter: value }),

  loadGrants: async () => {
    set({ grantsLoading: true, grantsError: null })
    try {
      const resp = await apiClient.get<{ grants?: GrantRecord[] }>(GRANTS_ENDPOINTS.grants_list, {
        params: filtersToParams(get().filters),
      })
      set({ grants: Array.isArray(resp.data?.grants) ? resp.data.grants : [] })
    } catch (e) {
      set({ grantsError: e instanceof Error ? e.message : String(e) })
    } finally {
      set({ grantsLoading: false })
    }
  },

  loadMetrics: async () => {
    set({ metricsError: null })
    try {
      const resp = await apiClient.get<MetricsResponse>(GRANTS_ENDPOINTS.grants_metrics)
      set({ metrics: resp.data ?? null })
    } catch (e) {
      set({ metricsError: e instanceof Error ? e.message : String(e) })
    }
  },

  loadAudit: async () => {
    set({ auditLoading: true })
    try {
      const auditType = get().auditTypeFilter
      const resp = await apiClient.get<{ events?: AuditEvent[] }>(GRANTS_ENDPOINTS.grants_audit, {
        params: auditType ? { event_type: auditType } : undefined,
      })
      set({ auditEvents: Array.isArray(resp.data?.events) ? resp.data.events : [] })
    } catch {
      // 审计流读取失败不阻断页面（列表/指标仍可用），事件区显示空态
      set({ auditEvents: [] })
    } finally {
      set({ auditLoading: false })
    }
  },

  loadProtoGrants: async () => {
    set({ protoError: null })
    try {
      const [wl, modes] = await Promise.all([
        apiClient.get<{ entries?: string[] }>(PIPELINE_SECURITY_CHECK_ENDPOINTS.whitelist_get),
        apiClient.get<{ entries?: Record<string, string> }>(
          PIPELINE_SECURITY_CHECK_ENDPOINTS.pipeline_permission_mode_get,
        ),
      ])
      set({
        whitelist: Array.isArray(wl.data?.entries) ? wl.data.entries : [],
        pipelineModes:
          modes.data?.entries && typeof modes.data.entries === 'object' ? modes.data.entries : {},
      })
    } catch (e) {
      set({ protoError: e instanceof Error ? e.message : String(e) })
    }
  },

  loadAll: async () => {
    await Promise.all([get().loadGrants(), get().loadMetrics(), get().loadAudit(), get().loadProtoGrants()])
  },

  revokeGrant: async (grantId) => {
    set({ revokingId: grantId })
    try {
      const url = GRANTS_ENDPOINTS.grants_revoke.replace('{grant_id}', encodeURIComponent(grantId))
      const resp = await apiClient.delete<{ revoked?: boolean; error?: string }>(url)
      if (resp.data?.error) throw new Error(resp.data.error)
      await Promise.all([get().loadGrants(), get().loadAudit(), get().loadMetrics()])
      return true
    } catch (e) {
      set({ grantsError: e instanceof Error ? e.message : String(e) })
      return false
    } finally {
      set({ revokingId: null })
    }
  },

  removeWhitelistEntry: async (signature) => {
    set({ protoError: null })
    try {
      const resp = await apiClient.post<{ removed?: boolean; error?: string }>(
        PIPELINE_SECURITY_CHECK_ENDPOINTS.whitelist_remove,
        { signature },
      )
      if (resp.data?.error) throw new Error(resp.data.error)
      await get().loadProtoGrants()
      return true
    } catch (e) {
      set({ protoError: e instanceof Error ? e.message : String(e) })
      return false
    }
  },

  revokePipelineMode: async (pipelineId) => {
    set({ protoError: null })
    try {
      const resp = await apiClient.delete<{ revoked?: boolean; error?: string }>(
        PIPELINE_SECURITY_CHECK_ENDPOINTS.pipeline_permission_mode_revoke,
        { data: { pipeline_id: pipelineId } },
      )
      if (resp.data?.error) throw new Error(resp.data.error)
      await get().loadProtoGrants()
      return true
    } catch (e) {
      set({ protoError: e instanceof Error ? e.message : String(e) })
      return false
    }
  },
}))

/** 审计流展示集：按当前类型过滤 + 最新在前 + 截尾 */
export function auditDisplayEvents(events: AuditEvent[], auditTypeFilter: string): AuditEvent[] {
  const filtered = auditTypeFilter ? events.filter((e) => e.event_type === auditTypeFilter) : events
  return filtered.slice(-AUDIT_DISPLAY_LIMIT).reverse()
}
