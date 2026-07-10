/**
 * 调试用户页面
 *
 * 展示用户调试信息
 */

import { useState, useEffect, useCallback } from 'react'
import * as usersApi from '@/services/api/users'
import type { User } from '@/services/api/users'

/**
 * 调试用户页面组件
 */
export function DebugUsersPage() {
  const [users, setUsers] = useState<User[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  /**
   * 加载用户列表
   */
  const fetchUsers = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const data = await usersApi.getUsers()
      setUsers(data)
    } catch (err: any) {
      setError(err.message || '获取用户列表失败')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchUsers()
  }, [fetchUsers])

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/debug" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">用户调试</h1>
        <span className="text-muted-foreground ml-auto text-xs">共 {users.length} 个用户</span>
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
        {!isLoading && !error && users.length === 0 && (
          <div className="text-muted-foreground py-12 text-center">暂无数据</div>
        )}

        {/* 用户列表 */}
        {!isLoading && !error && users.length > 0 && (
          <>
            {/* 移动端卡片视图 */}
            <div className="space-y-2 md:hidden">
              {users.map((user) => (
                <div
                  key={user.id}
                  className="cursor-pointer rounded-lg border p-3"
                  onClick={() => setExpandedId(expandedId === user.id ? null : user.id)}
                >
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm font-medium">{user.username}</span>
                    <div className="flex shrink-0 gap-1.5">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs ${
                          user.role === 'admin'
                            ? 'bg-status-info/10 text-status-info'
                            : 'bg-status-pending/10 text-status-pending'
                        }`}
                      >
                        {user.role}
                      </span>
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs ${
                          user.is_active
                            ? 'bg-status-success/10 text-status-success'
                            : 'bg-status-error/10 text-status-error'
                        }`}
                      >
                        {user.is_active ? '活跃' : '禁用'}
                      </span>
                    </div>
                  </div>
                  <div className="text-muted-foreground mt-2 space-y-1 text-xs">
                    <div>创建时间：{new Date(user.created_at).toLocaleString()}</div>
                    <div>最后登录：{user.last_login_at ? new Date(user.last_login_at).toLocaleString() : '--'}</div>
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
                      用户名
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      角色
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      状态
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      创建时间
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      最后登录
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((user) => (
                    <tr
                      key={user.id}
                      className="hover:bg-accent/20 cursor-pointer border-t"
                      onClick={() => setExpandedId(expandedId === user.id ? null : user.id)}
                    >
                      <td className="px-4 py-2 font-medium">{user.username}</td>
                      <td className="px-4 py-2">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs ${
                            user.role === 'admin'
                              ? 'bg-status-info/10 text-status-info'
                              : 'bg-status-pending/10 text-status-pending'
                          }`}
                        >
                          {user.role}
                        </span>
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs ${
                            user.is_active
                              ? 'bg-status-success/10 text-status-success'
                              : 'bg-status-error/10 text-status-error'
                          }`}
                        >
                          {user.is_active ? '活跃' : '禁用'}
                        </span>
                      </td>
                      <td className="text-muted-foreground px-4 py-2 text-xs">
                        {new Date(user.created_at).toLocaleString()}
                      </td>
                      <td className="text-muted-foreground px-4 py-2 text-xs">
                        {user.last_login_at ? new Date(user.last_login_at).toLocaleString() : '--'}
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
