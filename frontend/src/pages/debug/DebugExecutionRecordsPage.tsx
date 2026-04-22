/**
 * 调试执行记录页面
 *
 * 展示执行记录列表，支持按会话过滤
 */

import { useState, useEffect, useCallback } from 'react'
import {
  getExecutionRecords,
  getExecutionRecordsSessions,
} from '@/services/api/executionRecords'
import type {
  ExecutionRecord,
  SessionInfo,
} from '@/services/api/executionRecords'

/**
 * 获取记录状态样式
 */
function getRecordStatusStyle(status?: string): string {
  switch (status) {
    case 'completed':
      return 'bg-green-500/10 text-green-500'
    case 'running':
      return 'bg-blue-500/10 text-blue-500'
    case 'failed':
      return 'bg-red-500/10 text-red-500'
    case 'pending':
      return 'bg-yellow-500/10 text-yellow-500'
    default:
      return 'bg-gray-500/10 text-gray-500'
  }
}

/**
 * 调试执行记录页面组件
 */
export function DebugExecutionRecordsPage() {
  const [records, setRecords] = useState<ExecutionRecord[]>([])
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [selectedSession, setSelectedSession] = useState<string>('')
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

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
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/debug" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">执行记录</h1>
        <span className="ml-auto text-xs text-muted-foreground">共 {total} 条</span>
      </header>
      <main className="flex-1 overflow-y-auto p-6 space-y-4">
        {/* 会话过滤 */}
        <select
          value={selectedSession}
          onChange={e => handleSessionChange(e.target.value)}
          className="px-3 py-1.5 text-sm border rounded-lg bg-background"
        >
          <option value="">全部会话</option>
          {sessions.map(s => (
            <option key={s.id} value={s.id}>
              {s.title || s.id} ({s.record_count} 条)
            </option>
          ))}
        </select>

        {/* 加载状态 */}
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
            <span className="ml-2 text-sm text-muted-foreground">加载中...</span>
          </div>
        )}

        {/* 错误提示 */}
        {error && (
          <div className="p-4 rounded-lg bg-destructive/10 text-destructive text-sm">{error}</div>
        )}

        {/* 空状态 */}
        {!isLoading && !error && records.length === 0 && (
          <div className="text-center py-12 text-muted-foreground">暂无数据</div>
        )}

        {/* 记录列表 */}
        {!isLoading && !error && records.length > 0 && (
          <div className="border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-accent/30">
                <tr>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">ID</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">类型</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">状态</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">深度</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">创建时间</th>
                </tr>
              </thead>
              <tbody>
                {records.map(record => (
                  <tr key={record.id} className="border-t hover:bg-accent/20">
                    <td className="px-4 py-2 text-xs font-mono truncate max-w-[200px]">{record.id}</td>
                    <td className="px-4 py-2 text-xs">{record.record_type || '--'}</td>
                    <td className="px-4 py-2">
                      <span className={`text-xs px-2 py-0.5 rounded-full ${getRecordStatusStyle(record.status)}`}>
                        {record.status || '--'}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-xs">{record.depth ?? '--'}</td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {new Date(record.created_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </div>
  )
}
