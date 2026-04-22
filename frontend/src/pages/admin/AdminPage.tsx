/**
 * 管理员面板页面
 *
 * 用户管理，包含用户列表表格和用户统计
 */

import { useState, useEffect, useCallback } from 'react'
import * as usersApi from '@/services/api/users'
import type { User } from '@/services/api/users'

/**
 * 管理员面板页面组件
 */
export function AdminPage() {
  const [users, setUsers] = useState<User[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [stats, setStats] = useState<{ total_users: number; active_users: number; admin_count: number } | null>(null)

  /**
   * 加载用户数据
   */
  const fetchData = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const [userList, userStats] = await Promise.allSettled([
        usersApi.getUsers(),
        usersApi.getUserStats(),
      ])
      if (userList.status === 'fulfilled') {
        setUsers(userList.value)
      } else {
        setError('获取用户列表失败')
      }
      if (userStats.status === 'fulfilled') {
        setStats(userStats.value)
      }
    } catch (err: any) {
      setError(err.message || '加载数据失败')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  /**
   * 切换用户激活状态
   */
  const handleToggleActive = async (userId: string, currentActive: boolean) => {
    try {
      await usersApi.updateUserActiveStatus(userId, !currentActive)
      setUsers(prev =>
        prev.map(u => (u.id === userId ? { ...u, is_active: !currentActive } : u))
      )
    } catch {
      // 静默失败
    }
  }

  return (
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">管理员面板</h1>
      </header>
      <main className="flex-1 overflow-y-auto p-6 space-y-6">
        {/* 统计卡片 */}
        {stats && (
          <div className="grid grid-cols-3 gap-4">
            <div className="p-4 border rounded-lg">
              <div className="text-xs text-muted-foreground mb-1">总用户数</div>
              <div className="text-xl font-semibold">{stats.total_users}</div>
            </div>
            <div className="p-4 border rounded-lg">
              <div className="text-xs text-muted-foreground mb-1">活跃用户</div>
              <div className="text-xl font-semibold text-green-500">{stats.active_users}</div>
            </div>
            <div className="p-4 border rounded-lg">
              <div className="text-xs text-muted-foreground mb-1">管理员</div>
              <div className="text-xl font-semibold text-blue-500">{stats.admin_count}</div>
            </div>
          </div>
        )}

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

        {/* 用户列表 */}
        {!isLoading && !error && (
          <>
            {users.length === 0 ? (
              <div className="text-center py-12 text-muted-foreground">暂无数据</div>
            ) : (
              <div className="border rounded-lg overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-accent/30">
                    <tr>
                      <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">用户名</th>
                      <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">邮箱</th>
                      <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">角色</th>
                      <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">状态</th>
                      <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">创建时间</th>
                      <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {users.map(user => (
                      <tr key={user.id} className="border-t hover:bg-accent/20">
                        <td className="px-4 py-2 font-medium">{user.username}</td>
                        <td className="px-4 py-2 text-muted-foreground">{user.email || '--'}</td>
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
                        <td className="px-4 py-2">
                          <button
                            onClick={() => handleToggleActive(user.id, user.is_active)}
                            className="text-xs text-primary hover:underline"
                          >
                            {user.is_active ? '禁用' : '启用'}
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  )
}
