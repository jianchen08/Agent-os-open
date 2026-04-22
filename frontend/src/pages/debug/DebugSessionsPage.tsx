/**
 * 调试会话页面
 *
 * 展示调试会话列表
 */

import { useState, useEffect, useCallback } from 'react'
import { getExecutionRecordsSessions } from '@/services/api/executionRecords'
import type { SessionInfo } from '@/services/api/executionRecords'

/**
 * 调试会话页面组件
 */
export function DebugSessionsPage() {
  const [sessions, setSessions] = useState<SessionInfo[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  /**
   * 加载会话列表
   */
  const fetchSessions = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await getExecutionRecordsSessions()
      setSessions(res.sessions || [])
      setTotal(res.total)
    } catch (err: any) {
      setError(err.message || '获取会话列表失败')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchSessions()
  }, [fetchSessions])

  return (
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/debug" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">调试会话</h1>
        <span className="ml-auto text-xs text-muted-foreground">共 {total} 个会话</span>
      </header>
      <main className="flex-1 overflow-y-auto p-6 space-y-4">
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
        {!isLoading && !error && sessions.length === 0 && (
          <div className="text-center py-12 text-muted-foreground">暂无数据</div>
        )}

        {/* 会话列表 */}
        {!isLoading && !error && sessions.length > 0 && (
          <div className="border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-accent/30">
                <tr>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">会话 ID</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">标题</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">记录数</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">创建时间</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">更新时间</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map(session => (
                  <tr key={session.id} className="border-t hover:bg-accent/20">
                    <td className="px-4 py-2 text-xs font-mono truncate max-w-[180px]">{session.id}</td>
                    <td className="px-4 py-2 truncate max-w-[200px]">{session.title || '--'}</td>
                    <td className="px-4 py-2">
                      <span className="text-xs px-2 py-0.5 bg-primary/10 text-primary rounded-full">
                        {session.record_count}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {new Date(session.created_at).toLocaleString()}
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {new Date(session.updated_at).toLocaleString()}
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
