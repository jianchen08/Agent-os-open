/**
 * 管道 State 页面（调试中心）
 *
 * 以「管道 id」切换展示每个管道一页：pipeline_state 表全字段（不经
 * export_fields 白名单裁剪，field_value 为 DB 原始字符串、展示时尝试 JSON
 * pretty）+ 该管道全部 run + state 摘要行。数据面 = monitoring 插件
 * /ext/monitoring/pipeline-state。
 */

import { useMemo, useState } from 'react'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import {
  useDebugSessionsQuery,
  usePipelineStateFullQuery,
} from '@/hooks/queries/useDebugQueries'
import type { PipelineStateField } from '@/services/api/pipelineDiagnostics'

/** field_value 展示：JSON 形态 pretty 缩进，原文照显（解析失败不伪装） */
function formatFieldValue(raw: string | null): string {
  if (raw == null) return ''
  if (raw.length > 0 && (raw[0] === '{' || raw[0] === '[')) {
    try {
      return JSON.stringify(JSON.parse(raw), null, 2)
    } catch {
      return raw
    }
  }
  return raw
}

/** 单字段行：key / 值（长值折叠在 details 里）/ updated_at */
function StateFieldRow({ field }: { field: PipelineStateField }) {
  const pretty = formatFieldValue(field.field_value)
  const isLong = pretty.length > 120 || pretty.includes('\n')
  return (
    <tr className="border-t hover:bg-accent/20 align-top">
      <td className="px-3 py-2 font-mono text-xs break-all">{field.field_key}</td>
      <td className="max-w-[480px] px-3 py-2">
        {isLong ? (
          <details>
            <summary className="text-muted-foreground cursor-pointer text-xs">
              {pretty.length} 字符（展开）
            </summary>
            <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded bg-background p-2 font-mono text-xs">
              {pretty}
            </pre>
          </details>
        ) : (
          <span className="font-mono text-xs break-all">{pretty || '—'}</span>
        )}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground whitespace-nowrap">
        {field.updated_at ? new Date(field.updated_at).toLocaleString() : '—'}
      </td>
    </tr>
  )
}

/**
 * 管道 State 页面组件：管道选择器 + runs 表 + 全字段 state 表（键名搜索过滤）。
 */
export function DebugPipelineStatePage({ embedded }: { embedded?: boolean } = {}) {
  const [selectedPipeline, setSelectedPipeline] = useState<string>('')
  const [search, setSearch] = useState('')

  // 管道清单复用 execution sessions 读面（id 即 pipeline_id），带标题与消息数
  const sessionsQuery = useDebugSessionsQuery()
  const pipelines = sessionsQuery.data?.sessions ?? []
  const stateQuery = usePipelineStateFullQuery(selectedPipeline || undefined)
  const data = stateQuery.data
  const isLoading = stateQuery.isPending && !stateQuery.data && !!selectedPipeline
  const error = stateQuery.isError
    ? stateQuery.error instanceof Error
      ? stateQuery.error.message
      : '获取管道 state 失败'
    : null

  // 键名搜索：子串匹配（不区分大小写）
  const filteredFields = useMemo(() => {
    const fields = data?.fields ?? []
    const keyword = search.trim().toLowerCase()
    if (!keyword) return fields
    return fields.filter((f) => f.field_key.toLowerCase().includes(keyword))
  }, [data, search])

  const summary = data?.summary ?? null

  return (
    <PageShell title="管道 State" embedded={embedded}>
      {/* 管道选择器 + 键名搜索 */}
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={selectedPipeline}
          onChange={(e) => setSelectedPipeline(e.target.value)}
          className="bg-background rounded-lg border px-3 py-1.5 text-sm"
        >
          <option value="">选择管道</option>
          {pipelines.map((p) => (
            <option key={p.id} value={p.id}>
              {(p.title || p.id) + (p.run_status ? ` [${p.run_status}]` : '')}
            </option>
          ))}
        </select>
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="按字段名过滤（如 task / track / iteration）"
          className="bg-background w-64 rounded-lg border px-3 py-1.5 text-sm"
          disabled={!selectedPipeline}
        />
        {selectedPipeline && data && (
          <span className="text-muted-foreground text-xs">
            {data.fields.length} 个字段 · {data.runs.length} 次 run
          </span>
        )}
      </div>

      {/* 未选择管道的引导态 */}
      {!selectedPipeline && !error && (
        <div className="text-muted-foreground py-12 text-center text-sm">
          从上方选择一个管道，查看它的完整 state 字段、run 历史与摘要行。
        </div>
      )}

      {isLoading && <LoadingState />}
      {error && <ErrorState message={error} />}

      {selectedPipeline && data && !isLoading && !error && (
        <div className="space-y-4">
          {/* runs 历史表 */}
          {data.runs.length > 0 && (
            <div className="overflow-hidden rounded-lg border">
              <table className="w-full text-sm">
                <thead className="bg-accent/30">
                  <tr>
                    <th className="text-muted-foreground px-3 py-2 text-left text-xs font-medium">run_id</th>
                    <th className="text-muted-foreground px-3 py-2 text-left text-xs font-medium">状态</th>
                    <th className="text-muted-foreground px-3 py-2 text-left text-xs font-medium">开始</th>
                    <th className="text-muted-foreground px-3 py-2 text-left text-xs font-medium">结束</th>
                  </tr>
                </thead>
                <tbody>
                  {data.runs.map((r) => (
                    <tr key={r.run_id} className="border-t">
                      <td className="max-w-[220px] truncate px-3 py-2 font-mono text-xs">{r.run_id}</td>
                      <td className="px-3 py-2 text-xs">{r.status || '—'}</td>
                      <td className="text-muted-foreground px-3 py-2 text-xs">
                        {r.started_at ? new Date(r.started_at).toLocaleString() : '—'}
                      </td>
                      <td className="text-muted-foreground px-3 py-2 text-xs">
                        {r.ended_at ? new Date(r.ended_at).toLocaleString() : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* state 摘要行（内存热数据，若在） */}
          {summary && (
            <div className="rounded-lg border p-3">
              <div className="text-muted-foreground mb-1 text-xs font-medium">state 摘要行</div>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-background p-2 font-mono text-xs">
                {JSON.stringify(summary, null, 2)}
              </pre>
            </div>
          )}

          {/* state 全字段表 */}
          <div className="overflow-hidden rounded-lg border">
            <table className="w-full text-sm">
              <thead className="bg-accent/30">
                <tr>
                  <th className="text-muted-foreground w-1/3 px-3 py-2 text-left text-xs font-medium">字段</th>
                  <th className="text-muted-foreground px-3 py-2 text-left text-xs font-medium">值</th>
                  <th className="text-muted-foreground px-3 py-2 text-left text-xs font-medium">更新时间</th>
                </tr>
              </thead>
              <tbody>
                {filteredFields.length === 0 ? (
                  <tr>
                    <td colSpan={3} className="text-muted-foreground px-3 py-8 text-center text-sm">
                      {data.fields.length === 0 ? '该管道暂无 state 字段' : '无匹配字段'}
                    </td>
                  </tr>
                ) : (
                  filteredFields.map((f) => <StateFieldRow key={f.field_key} field={f} />)
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </PageShell>
  )
}
