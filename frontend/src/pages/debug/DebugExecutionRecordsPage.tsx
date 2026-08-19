/**
 * 调试执行记录页面
 *
 * 展示执行记录列表，支持按会话过滤
 */

import { useState, useEffect, useCallback, Fragment } from 'react'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import { getExecutionRecords, getExecutionRecordsSessions } from '@/services/api/executionRecords'
import type { ExecutionRecord, SessionInfo } from '@/services/api/executionRecords'

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

/**
 * 调试执行记录页面组件
 *
 * 2026-08-19：后端已接内核消息快照（message_slots⨝blobs 读时重建），
 * message_data 携带全文/tool_calls/reasoning——本页展开渲染拼装后的消息内容。
 */
export function DebugExecutionRecordsPage({ embedded }: { embedded?: boolean } = {}) {
  const [records, setRecords] = useState<ExecutionRecord[]>([])
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [selectedSession, setSelectedSession] = useState<string>('')
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  /**
   * 加载会话列表
   */
  const fetchSessions = useCallback(async () => {
    try {
      const res = await getExecutionRecordsSessions()
      setSessions(res.sessions || [])
    } catch {
      // 会话列表加载失败不阻塞
    }
  }, [])

  /**
   * 加载执行记录
   */
  const fetchRecords = useCallback(async (sessionId?: string) => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await getExecutionRecords({
        session_id: sessionId || undefined,
        limit: 50,
      })
      setRecords(res.records)
      setTotal(res.total)
    } catch (err: any) {
      setError(err.message || '获取执行记录失败')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchSessions()
    fetchRecords()
  }, [fetchSessions, fetchRecords])

  /** 切换会话过滤 */
  const handleSessionChange = (sessionId: string) => {
    setSelectedSession(sessionId)
    fetchRecords(sessionId || undefined)
  }

  return (
    <PageShell
      title="执行记录"
      backHref="/debug"
      embedded={embedded}
      actions={<span className="text-muted-foreground text-xs">共 {total} 条</span>}
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
