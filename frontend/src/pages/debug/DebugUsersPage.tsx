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
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/debug" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">用户调试</h1>
        <span className="ml-auto text-xs text-muted-foreground">共 {users.length} 个用户</span>
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
        {!isLoading && !error && users.length === 0 && (
          <div className="text-center py-12 text-muted-foreground">暂无数据</div>
        )}

        {/* 用户列表 */}
        {!isLoading && !error && users.length > 0 && (
          <div className="border rounded-lg overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-accent/30">
                <tr>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">用户名</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">角色</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">状态</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">创建时间</th>
                  <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">最后登录</th>
                </tr>
              </thead>
              <tbody>
                {users.map(user => (
                  <tr
                    key={user.id}
                    className="border-t hover:bg-accent/20 cursor-pointer"
                    onClick={() => setExpandedId(expandedId === user.id ? null : user.id)}
                  >
                    <td className="px-4 py-2 font-medium">{user.username}</td>
                    <td className="px-4 py-2">
                      <span className={`text-xs px-2 py-0.5 rounded-full ${
                        user.role === 'admin'
                          ? 'bg-blue-500/10 text-blue-500'
                          : 'bg-gray-500/10 text-gray-500'
                      }`}>
                        {user.role}
                      </span>
                    </td>
                    <td className="px-4 py-2">
                      <span className={`text-xs px-2 py-0.5 rounded-full ${
                        user.is_active
                          ? 'bg-green-500/10 text-green-500'
                          : 'bg-red-500/10 text-red-500'
                      }`}>
                        {user.is_active ? '活跃' : '禁用'}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {new Date(user.created_at).toLocaleString()}
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {user.last_login_at ? new Date(user.last_login_at).toLocaleString() : '--'}
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
