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
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/debug" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">调试会话</h1>
        <span className="text-muted-foreground ml-auto text-xs">共 {total} 个会话</span>
      </header>
      <main className="flex-1 space-y-4 overflow-y-auto p-3 sm:p-6">
        {/* 加载状态 */}
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="border-primary h-6 w-6 animate-spin rounded-full border-2 border-t-transparent" />
            <span className="text-muted-foreground ml-2 text-sm">加载中...</span>
          </div>
        )}

        {/* 错误提示 */}
        {error && (
          <div className="bg-destructive/10 text-destructive rounded-lg p-4 text-sm">{error}</div>
        )}

        {/* 空状态 */}
        {!isLoading && !error && sessions.length === 0 && (
          <div className="text-muted-foreground py-12 text-center">暂无数据</div>
        )}

        {/* 会话列表 */}
        {!isLoading && !error && sessions.length > 0 && (
          <>
            {/* 移动端卡片视图 */}
            <div className="space-y-2 md:hidden">
              {sessions.map((session) => (
                <div key={session.id} className="rounded-lg border p-3">
                  <div className="text-sm font-medium truncate">{session.title || session.id}</div>
                  <div className="text-muted-foreground mt-1 font-mono text-xs truncate">{session.id}</div>
                  <div className="text-muted-foreground mt-2 space-y-1 text-xs">
                    <div className="flex items-center gap-2">
                      <span>记录数：</span>
                      <span className="bg-primary/10 text-primary rounded-full px-2 py-0.5 text-xs">
                        {session.record_count}
                      </span>
                    </div>
                    <div>创建：{new Date(session.created_at).toLocaleString()}</div>
                    <div>更新：{new Date(session.updated_at).toLocaleString()}</div>
                  </div>
                </div>
              ))}
            </div>
            {/* 桌面端表格视图 */}
            <div className="hidden md:block overflow-hidden rounded-lg border">
              <table className="w-full text-sm">
                <thead className="bg-accent/30">
                  <tr>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      会话 ID
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      标题
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      记录数
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      创建时间
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      更新时间
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((session) => (
                    <tr key={session.id} className="hover:bg-accent/20 border-t">
                      <td className="max-w-[180px] truncate px-4 py-2 font-mono text-xs">
                        {session.id}
                      </td>
                      <td className="max-w-[200px] truncate px-4 py-2">{session.title || '--'}</td>
                      <td className="px-4 py-2">
                        <span className="bg-primary/10 text-primary rounded-full px-2 py-0.5 text-xs">
                          {session.record_count}
                        </span>
                      </td>
                      <td className="text-muted-foreground px-4 py-2 text-xs">
                        {new Date(session.created_at).toLocaleString()}
                      </td>
                      <td className="text-muted-foreground px-4 py-2 text-xs">
                        {new Date(session.updated_at).toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </main>
    </div>
  )
}
