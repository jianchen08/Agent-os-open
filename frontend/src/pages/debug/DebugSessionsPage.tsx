/**
 * 调试会话页面（query 化：useDebugSessionsQuery 缓存 SWR，重挂零请求）
 *
 * 展示调试会话列表
 */

import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import { useDebugSessionsQuery } from '@/hooks/queries/useDebugQueries'

/**
 * 调试会话页面组件
 */
export function DebugSessionsPage({ embedded }: { embedded?: boolean } = {}) {
  const sessionsQuery = useDebugSessionsQuery()
  const sessions = sessionsQuery.data?.sessions ?? []
  const total = sessionsQuery.data?.total ?? 0
  // 无缓存数据时显示 loading（有缓存先渲染缓存不闪 loading）
  const isLoading = sessionsQuery.isPending && !sessionsQuery.data
  const error = sessionsQuery.isError
    ? sessionsQuery.error instanceof Error
      ? sessionsQuery.error.message
      : '获取会话列表失败'
    : null

  return (
    <PageShell
      title="调试会话"
      backHref="/debug"
      embedded={embedded}
      actions={<span className="text-muted-foreground text-xs">共 {total} 个会话</span>}
    >
      {/* 加载状态 */}
      {isLoading && <LoadingState />}

      {/* 错误提示 */}
      {error && <ErrorState message={error} />}

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
                      {session.record_count != null && (
                        <span className="bg-primary/10 text-primary rounded-full px-2 py-0.5 text-xs">
                          {session.record_count}
                        </span>
                      )}
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
    </PageShell>
  )
}
