/**
 * 调试执行 Trace 页面（query 化：双 useQuery 缓存 SWR，重挂零请求）
 *
 * 以「管道」为坐标展示 step 级执行轨迹（traces 表投影：插件名/patch 类型/
 * 轮次/摘要/token/错误，可展开原始 patch_data），按轮次分组渲染；
 * 支持按管道过滤；「清空全部」一键清理所有执行记录与轨迹。
 */

import { useQueryClient } from '@tanstack/react-query'
import { useState, useCallback, useMemo } from 'react'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import {
  useDebugSessionsQuery,
  usePipelineTracesQuery,
} from '@/hooks/queries/useDebugQueries'
import { clearAllExecutionRecords } from '@/services/api/executionRecords'
import { queryKeys } from '@/services/query/queryKeys'
import type { PipelineTraceRow } from '@/services/api/pipelineDiagnostics'

/** patch 类型徽章配色（error/rollback 醒目，其余中性） */
function getPatchTypeStyle(patchType: string): string {
  switch (patchType) {
    case 'error':
      return 'bg-status-error/10 text-status-error'
    case 'rollback':
      return 'bg-status-warning/10 text-status-warning'
    default:
      return 'bg-accent/40 text-muted-foreground'
  }
}

/** 单条 trace 行：插件/类型/摘要/token/错误 + 可展开原始 patch */
function TraceRow({ trace }: { trace: PipelineTraceRow }) {
  const usage = trace.llm_usage
  return (
    <div className="rounded-lg border p-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-mono text-muted-foreground">#{trace.seq ?? '?'}</span>
        <span className="rounded bg-primary/10 px-1.5 py-0.5 font-mono text-primary">
          {trace.plugin_id}
        </span>
        <span className={`rounded px-1.5 py-0.5 ${getPatchTypeStyle(trace.patch_type)}`}>
          {trace.patch_type}
        </span>
        {trace.iteration != null && (
          <span className="text-muted-foreground">轮 {trace.iteration}</span>
        )}
        {trace.tool_call_count > 0 && (
          <span className="text-muted-foreground">工具 ×{trace.tool_call_count}</span>
        )}
        {usage && (usage.total_tokens != null || usage.model) && (
          <span className="text-muted-foreground">
            {/* 数字字段可缺（空 LLM 轮只带归属键）：缺数不渲染 token 段，不猜 0 */}
            {usage.total_tokens != null
              ? `${usage.total_tokens.toLocaleString()} tok${usage.model ? ` · ${usage.model}` : ''}`
              : usage.model}
          </span>
        )}
        <span className="ml-auto text-muted-foreground">
          {trace.created_at ? new Date(trace.created_at).toLocaleTimeString() : ''}
        </span>
      </div>
      {trace.error && (
        <div className="mt-1 rounded bg-status-error/10 p-1.5 text-xs text-status-error break-all">
          {trace.error.length > 300 ? `${trace.error.slice(0, 300)}…` : trace.error}
        </div>
      )}
      {trace.summary && (
        <div className="text-muted-foreground mt-1 text-xs break-all">
          {trace.summary.length > 200 ? `${trace.summary.slice(0, 200)}…` : trace.summary}
        </div>
      )}
      <details className="mt-1">
        <summary className="text-muted-foreground cursor-pointer text-xs">原始 patch</summary>
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-background p-2 font-mono text-xs">
          {JSON.stringify(trace.patch_data, null, 2)}
        </pre>
      </details>
    </div>
  )
}

/** trace 时间线：按轮次分组（iteration 缺失的步归「前置 / 后置步」组） */
function TraceTimeline({ traces }: { traces: PipelineTraceRow[] }) {
  const groups = useMemo(() => {
    const map = new Map<string, PipelineTraceRow[]>()
    for (const t of traces) {
      const key = t.iteration != null ? `轮 ${t.iteration}` : '前置 / 后置步'
      const bucket = map.get(key)
      if (bucket) bucket.push(t)
      else map.set(key, [t])
    }
    return Array.from(map.entries())
  }, [traces])

  return (
    <div className="space-y-3">
      {groups.map(([label, rows]) => (
        <div key={label}>
          <div className="text-muted-foreground mb-1 border-b pb-1 text-xs font-medium">
            {label}
          </div>
          <div className="space-y-2">
            {rows.map((t) => (
              <TraceRow key={t.trace_id} trace={t} />
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

/** 从清理错误中提取可展示消息（axios 信封 detail 优先，409 运行中提示等） */
function extractClearError(e: unknown): string {
  if (e && typeof e === 'object' && 'response' in e) {
    const data = (e as { response?: { data?: { detail?: unknown; error?: unknown } } }).response
      ?.data
    const detail = data?.detail ?? data?.error
    if (detail) return String(detail)
  }
  return e instanceof Error ? e.message : '清理失败'
}

/**
 * 调试执行 Trace 页面组件
 *
 * 数据面 = /ext/monitoring/traces（step 级轨迹投影）；会话下拉的 id 即管道 id
 * （execution sessions 读面的 session 坐标本就是 pipeline_id），选中即查该管道轨迹。
 */
export function DebugExecutionRecordsPage({ embedded }: { embedded?: boolean } = {}) {
  const [selectedPipeline, setSelectedPipeline] = useState<string>('')
  const [clearing, setClearing] = useState(false)
  const [clearMessage, setClearMessage] = useState<string | null>(null)
  const [clearError, setClearError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  // 管道列表 + 选中管道的 trace 时间线（query 化）：管道 id 进 key，切换 = 换缓存条目
  const sessionsQuery = useDebugSessionsQuery()
  const pipelines = sessionsQuery.data?.sessions ?? []
  const tracesQuery = usePipelineTracesQuery(selectedPipeline || undefined)
  const traces = tracesQuery.data?.traces ?? []
  const total = tracesQuery.data?.total ?? 0
  // 无缓存数据时显示 loading（有缓存先渲染缓存不闪 loading）
  const isLoading = tracesQuery.isPending && !tracesQuery.data && !!selectedPipeline
  const error = tracesQuery.isError
    ? tracesQuery.error instanceof Error
      ? tracesQuery.error.message
      : '获取执行轨迹失败'
    : null

  /** 清空全部执行记录与轨迹（confirm 二次确认；成功后失效受影响缓存） */
  const handleClearAll = useCallback(async () => {
    if (
      !window.confirm(
        '确定清空全部执行记录与轨迹？\n\n' +
          '将删除：执行记录、消息、任务轨迹、管道状态与 LLM 请求快照（用户账号保留）。\n' +
          '数据库会自动生成清理前备份。此操作不可撤销。',
      )
    ) {
      return
    }
    setClearing(true)
    setClearError(null)
    setClearMessage(null)
    try {
      const result = await clearAllExecutionRecords()
      setClearMessage(
        `已清理 ${result.cleared_count} 条记录` +
          (result.payload_files_deleted ? `、${result.payload_files_deleted} 个 LLM 请求快照` : '') +
          (result.backup_path ? '（已自动备份）' : ''),
      )
      setSelectedPipeline('')
      // 失效所有数据源为执行数据的缓存（前缀失效覆盖分条 key）
      for (const key of [
        queryKeys.executionRecordsPrefix,
        queryKeys.pipelineTracesPrefix,
        queryKeys.pipelineStateFullPrefix,
        queryKeys.debugSessions,
        queryKeys.debugTasks,
        queryKeys.llmPayloadDiagPrefix,
        queryKeys.sessions,
        queryKeys.pipelineAllTasks,
        queryKeys.longTermTasks,
        queryKeys.pipelineRuns,
        queryKeys.pipelineStates,
      ]) {
        void queryClient.invalidateQueries({ queryKey: key })
      }
    } catch (e) {
      setClearError(extractClearError(e))
    } finally {
      setClearing(false)
    }
  }, [queryClient])

  return (
    <PageShell
      title="执行 Trace"
      embedded={embedded}
      actions={
        <span className="flex items-center gap-3">
          {selectedPipeline && (
            <span className="text-muted-foreground text-xs">共 {total} 步</span>
          )}
          <button
            onClick={handleClearAll}
            disabled={clearing}
            className="rounded-lg bg-status-error/10 px-3 py-1.5 text-xs text-status-error hover:bg-status-error/20 disabled:opacity-50"
          >
            {clearing ? '清理中…' : '清空全部'}
          </button>
        </span>
      }
    >
      {/* 管道过滤 */}
      <select
        value={selectedPipeline}
        onChange={(e) => setSelectedPipeline(e.target.value)}
        className="bg-background rounded-lg border px-3 py-1.5 text-sm"
      >
        <option value="">选择管道查看执行轨迹</option>
        {pipelines.map((p) => (
          <option key={p.id} value={p.id}>
            {(p.title || p.id) + (p.record_count != null ? ` (${p.record_count} 条消息)` : '')}
          </option>
        ))}
      </select>

      {/* 清理结果反馈 */}
      {clearMessage && (
        <div className="bg-status-success/10 rounded-lg p-2 text-xs text-status-success">
          {clearMessage}
        </div>
      )}
      {clearError && <ErrorState message={clearError} />}

      {/* 未选择管道的引导态 */}
      {!selectedPipeline && !clearError && (
        <div className="text-muted-foreground py-12 text-center text-sm">
          从上方选择一个管道，查看它的 step 级执行轨迹（插件 / 轮次 / token / 错误）。
        </div>
      )}

      {/* 加载状态 */}
      {isLoading && <LoadingState />}

      {/* 错误提示 */}
      {error && <ErrorState message={error} />}

      {/* 空状态 */}
      {selectedPipeline && !isLoading && !error && traces.length === 0 && (
        <div className="text-muted-foreground py-12 text-center">该管道暂无轨迹数据</div>
      )}

      {/* trace 时间线 */}
      {selectedPipeline && !isLoading && !error && traces.length > 0 && (
        <TraceTimeline traces={traces} />
      )}
    </PageShell>
  )
}
