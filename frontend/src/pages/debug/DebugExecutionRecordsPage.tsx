/**
 * 调试执行记录页面（query 化：双 useQuery 缓存 SWR，重挂零请求）
 *
 * 展示执行记录列表，支持按会话过滤；「清空全部」一键清理所有执行记录与
 * 轨迹（内核 9 表 + LLM 请求快照文件，users 保留，自动备份）。
 */

import { useQueryClient } from '@tanstack/react-query'
import { useState, useCallback, Fragment } from 'react'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import { useDebugSessionsQuery, useExecutionRecordsQuery } from '@/hooks/queries/useDebugQueries'
import {
  clearAllExecutionRecords,
  type ExecutionRecord,
} from '@/services/api/executionRecords'
import { queryKeys } from '@/services/query/queryKeys'

/**
 * 获取记录状态样式
 */
function getRecordStatusStyle(status?: string): string {
  switch (status) {
    case 'completed':
      return 'bg-status-success/10 text-status-success'
    case 'running':
      return 'bg-status-info/10 text-status-info'
    case 'failed':
      return 'bg-status-error/10 text-status-error'
    case 'pending':
      return 'bg-status-warning/10 text-status-warning'
    default:
      return 'bg-status-pending/10 text-status-pending'
  }
}

/**
 * 从 message_data 提取纯文本内容（content 可能是 string 或分段数组）
 */
function extractContentText(messageData: Record<string, unknown> | undefined): string {
  const content = messageData?.content
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === 'string') return part
        if (part && typeof part === 'object' && 'text' in (part as Record<string, unknown>)) {
          return String((part as Record<string, unknown>).text)
        }
        return ''
      })
      .filter(Boolean)
      .join('\n')
  }
  return ''
}

/**
 * 消息快照展开面板：渲染拼装后的消息内容（全文/工具调用/思考/错误）
 */
function MessageSnapshotDetail({ record }: { record: ExecutionRecord }) {
  const md = record.message_data as Record<string, unknown> | undefined
  const content = extractContentText(md)
  const toolCalls = md?.tool_calls as Array<Record<string, any>> | null | undefined
  const reasoning = md?.reasoning_content as string | null | undefined
  const toolError = md?.error as string | null | undefined
  const toolCallId = md?.tool_call_id as string | null | undefined

  return (
    <div className="mt-2 space-y-2 rounded-lg bg-accent/20 p-3 text-xs">
      {toolCallId && (
        <div className="text-muted-foreground font-mono">tool_call_id: {toolCallId}</div>
      )}
      {content && (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 font-mono">
          {content}
        </pre>
      )}
      {toolCalls && toolCalls.length > 0 && (
        <div className="space-y-1">
          <div className="text-muted-foreground">工具调用（{toolCalls.length}）：</div>
          {toolCalls.map((tc, i) => (
            <div key={tc.id ?? i} className="bg-background rounded p-2 font-mono break-all">
              {tc.function?.name ?? tc.name ?? `call-${i}`}
              {tc.function?.arguments && (
                <span className="text-muted-foreground"> {String(tc.function.arguments).slice(0, 300)}</span>
              )}
            </div>
          ))}
        </div>
      )}
      {reasoning && (
        <details>
          <summary className="text-muted-foreground cursor-pointer">思考过程（{reasoning.length} 字符）</summary>
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 font-mono">
            {reasoning}
          </pre>
        </details>
      )}
      {toolError && (
        <div className="rounded bg-status-error/10 p-2 text-status-error break-all">{toolError}</div>
      )}
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
 * 调试执行记录页面组件
 *
 * 后端已接内核消息快照（message_slots⨝blobs 读时重建），
 * message_data 携带全文/tool_calls/reasoning——本页展开渲染拼装后的消息内容。
 */
export function DebugExecutionRecordsPage({ embedded }: { embedded?: boolean } = {}) {
  const [selectedSession, setSelectedSession] = useState<string>('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [clearing, setClearing] = useState(false)
  const [clearMessage, setClearMessage] = useState<string | null>(null)
  const [clearError, setClearError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  // 会话列表 + 执行记录（query 化）：sessionId 进 key，切换过滤 = 换缓存条目
  const sessionsQuery = useDebugSessionsQuery()
  const sessions = sessionsQuery.data?.sessions ?? []
  const recordsQuery = useExecutionRecordsQuery(selectedSession || undefined)
  const records = recordsQuery.data?.records ?? []
  const total = recordsQuery.data?.total ?? 0
  // 无缓存数据时显示 loading（有缓存先渲染缓存不闪 loading）
  const isLoading = recordsQuery.isPending && !recordsQuery.data
  const error = recordsQuery.isError
    ? recordsQuery.error instanceof Error
      ? recordsQuery.error.message
      : '获取执行记录失败'
    : null

  /** 切换会话过滤 */
  const handleSessionChange = (sessionId: string) => {
    setSelectedSession(sessionId)
  }

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
      setSelectedSession('')
      // 失效所有数据源为执行数据的缓存（前缀失效覆盖分条 key）
      for (const key of [
        queryKeys.executionRecordsPrefix,
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
      title="执行记录"
      backHref="/debug"
      embedded={embedded}
      actions={
        <span className="flex items-center gap-3">
          <span className="text-muted-foreground text-xs">共 {total} 条</span>
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
      {/* 会话过滤 */}
      <select
        value={selectedSession}
        onChange={(e) => handleSessionChange(e.target.value)}
        className="bg-background rounded-lg border px-3 py-1.5 text-sm"
      >
        <option value="">全部会话</option>
        {sessions.map((s) => (
          <option key={s.id} value={s.id}>
            {s.title || s.id}
            {s.record_count != null ? ` (${s.record_count} 条)` : ''}
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

      {/* 加载状态 */}
      {isLoading && <LoadingState />}

      {/* 错误提示 */}
      {error && <ErrorState message={error} />}

      {/* 空状态 */}
      {!isLoading && !error && records.length === 0 && (
        <div className="text-muted-foreground py-12 text-center">暂无数据</div>
      )}

      {/* 记录列表 */}
      {!isLoading && !error && records.length > 0 && (
        <>
          {/* 移动端卡片视图 */}
          <div className="space-y-2 md:hidden">
              {records.map((record) => (
                <div key={record.id} className="rounded-lg border p-3">
                  <div className="flex items-start justify-between gap-2">
                    <span className="max-w-[180px] truncate font-mono text-xs">{record.id}</span>
                    <span
                      className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${getRecordStatusStyle(record.status)}`}
                    >
                      {record.status || '--'}
                    </span>
                  </div>
                  <div className="text-muted-foreground mt-2 space-y-1 text-xs">
                    <div>类型：{record.record_type || '--'}</div>
                    <div>深度：{record.depth ?? '--'}</div>
                    <div>创建时间：{new Date(record.created_at).toLocaleString()}</div>
                  </div>
                  <button
                    onClick={() => setExpandedId(expandedId === record.id ? null : record.id)}
                    className="text-primary mt-2 text-xs hover:underline"
                  >
                    {expandedId === record.id ? '收起内容' : '展开消息内容'}
                  </button>
                  {expandedId === record.id && <MessageSnapshotDetail record={record} />}
                </div>
              ))}
            </div>
            {/* 桌面端表格视图 */}
            <div className="hidden md:block overflow-hidden rounded-lg border">
              <table className="w-full text-sm">
                <thead className="bg-accent/30">
                  <tr>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      ID
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      类型
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      内容
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      状态
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      会话
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      创建时间
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((record) => (
                    <Fragment key={record.id}>
                      <tr
                        className="hover:bg-accent/20 border-t cursor-pointer"
                        onClick={() => setExpandedId(expandedId === record.id ? null : record.id)}
                      >
                        <td className="max-w-[160px] truncate px-4 py-2 font-mono text-xs">
                          {record.id}
                        </td>
                        <td className="px-4 py-2 text-xs">{record.record_type || '--'}</td>
                        <td className="max-w-[320px] truncate px-4 py-2 text-xs">
                          {extractContentText(record.message_data) || (
                            <span className="text-muted-foreground">
                              {Array.isArray((record.message_data as any)?.tool_calls)
                                ? `工具调用 ×${(record.message_data as any).tool_calls.length}`
                                : '--'}
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-2">
                          <span
                            className={`rounded-full px-2 py-0.5 text-xs ${getRecordStatusStyle(record.status)}`}
                          >
                            {record.status || '--'}
                          </span>
                        </td>
                        <td className="max-w-[120px] truncate px-4 py-2 font-mono text-xs text-muted-foreground">
                          {record.session_id?.slice(0, 10) || '--'}
                        </td>
                        <td className="text-muted-foreground px-4 py-2 text-xs">
                          {new Date(record.created_at).toLocaleString()}
                        </td>
                      </tr>
                      {expandedId === record.id && (
                        <tr className="border-t">
                          <td colSpan={6} className="px-4 py-2">
                            <MessageSnapshotDetail record={record} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
    </PageShell>
  )
}
