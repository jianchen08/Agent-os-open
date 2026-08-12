/**
 * 管理员面板页面
 *
 * 用户管理，包含用户列表表格和用户统计
 */

import { useState, useEffect, useCallback } from 'react'
import { Users } from '@/assets/icons'
import { EmptyState } from '@/components/shared/EmptyState'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import * as usersApi from '@/services/api/users'
import type { User } from '@/services/api/users'

/**
 * 管理员面板页面组件
 */
export function AdminPage() {
  const [users, setUsers] = useState<User[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [stats, setStats] = useState<{
    total_users: number
    active_users: number
    admin_count: number
  } | null>(null)

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
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '加载数据失败'
      setError(message)
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
      setUsers((prev) =>
        prev.map((u) => (u.id === userId ? { ...u, is_active: !currentActive } : u)),
      )
    } catch {
      // 静默失败
    }
  }

  return (
    <PageShell title="管理员面板" backHref="/">
      {/* 统计卡片 */}
      {stats && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <div className="rounded-lg border p-4">
            <div className="text-muted-foreground mb-1 text-xs">总用户数</div>
            <div className="text-xl font-semibold">{stats.total_users}</div>
          </div>
          <div className="rounded-lg border p-4">
            <div className="text-muted-foreground mb-1 text-xs">活跃用户</div>
            <div className="text-xl font-semibold text-status-success">{stats.active_users}</div>
          </div>
          <div className="rounded-lg border p-4">
            <div className="text-muted-foreground mb-1 text-xs">管理员</div>
            <div className="text-xl font-semibold text-status-info">{stats.admin_count}</div>
          </div>
        </div>
      )}

      {/* 加载状态 */}
      {isLoading && <LoadingState />}

      {/* 错误提示 */}
      {error && <ErrorState message={error} />}

      {/* 用户列表 */}
      {!isLoading && !error && (
        <>
          {users.length === 0 ? (
            <EmptyState
              icon={Users}
              title="暂无用户"
              description="用户注册后将在这里显示"
            />
          ) : (
              <>
                {/* 移动端卡片视图 */}
                <div className="space-y-2 md:hidden">
                  {users.map((user) => (
                    <div key={user.id} className="rounded-lg border p-3">
                      <div className="flex items-start justify-between gap-2">
                        <span className="text-sm font-medium">{user.username}</span>
                        <span
                          className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${
                            user.is_active
                              ? 'bg-status-success/10 text-status-success'
                              : 'bg-status-error/10 text-status-error'
                          }`}
                        >
                          {user.is_active ? '活跃' : '禁用'}
                        </span>
                      </div>
                      <div className="text-muted-foreground mt-2 space-y-1 text-xs">
                        <div>邮箱：{user.email || '--'}</div>
                        <div className="flex items-center gap-2">
                          <span>角色：</span>
                          <span
                            className={`rounded-full px-2 py-0.5 text-xs ${
                              user.role === 'admin'
                                ? 'bg-status-info/10 text-status-info'
                                : 'bg-muted-foreground/10 text-muted-foreground'
                            }`}
                          >
                            {user.role}
                          </span>
                        </div>
                        <div>创建时间：{new Date(user.created_at).toLocaleString()}</div>
                      </div>
                      <div className="mt-2">
                        <button
                          onClick={() => handleToggleActive(user.id, user.is_active)}
                          className="text-primary text-xs hover:underline"
                          aria-label={user.is_active ? `禁用用户 ${user.username}` : `启用用户 ${user.username}`}
                        >
                          {user.is_active ? '禁用' : '启用'}
                        </button>
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
                          邮箱
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
                          操作
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {users.map((user) => (
                        <tr key={user.id} className="hover:bg-accent/20 border-t">
                          <td className="px-4 py-2 font-medium">{user.username}</td>
                          <td className="text-muted-foreground px-4 py-2">{user.email || '--'}</td>
                          <td className="px-4 py-2">
                            <span
                              className={`rounded-full px-2 py-0.5 text-xs ${
                                user.role === 'admin'
                                  ? 'bg-status-info/10 text-status-info'
                                  : 'bg-muted-foreground/10 text-muted-foreground'
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
                          <td className="px-4 py-2">
                            <button
                              onClick={() => handleToggleActive(user.id, user.is_active)}
                              className="text-primary text-xs hover:underline"
                              aria-label={user.is_active ? `禁用用户 ${user.username}` : `启用用户 ${user.username}`}
                            >
                              {user.is_active ? '禁用' : '启用'}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </>
        )}
    </PageShell>
  )
}
