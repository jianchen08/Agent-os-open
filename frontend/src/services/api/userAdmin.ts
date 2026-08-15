/**
 * 用户管理策略面 API 客户端（/ext/user_admin/*）
 *
 * boot-plugin 第二刀（§9.6 精确拆分）：auth 执行门（login/logout/me/register/
 * refresh，见 auth.ts）永留内核；本客户端只覆盖**管理性质**的用户管理策略面
 * ——用户列表/改角色/改租户/删用户，HTTP 面在 user_admin 插件
 * （plugins/shared/user_admin），数据与鉴权在内核 user-admin capability
 * handler（admin 角色 + self-service 防护：不能删自己/降自己角色/改自己租户）。
 *
 * 注意：用户管理响应**永不包含 password 字段**（内核 handler 已剥离）。
 * 消费方（管理页面）后续接入；契约与 dbAdmin.ts 风格对齐。
 */

import apiClient from '@/services/api/client'

/** 用户管理条目（GET /ext/user_admin/users 条目；不含密码） */
export interface UserAdminItem {
  id: string
  username: string
  email?: string
  role: 'admin' | 'user'
  tenant_id: string
  created_at: string
  last_login_at?: string
}

/** 用户列表结果（GET /ext/user_admin/users） */
export interface UserAdminListResult {
  users: UserAdminItem[]
  total: number
}

/** 变更结果（PATCH role/tenant；返回更新后的用户，不含密码） */
export interface UserAdminUpdateResult {
  user: UserAdminItem
}

/** 删除结果（DELETE /ext/user_admin/users/{id}） */
export interface UserAdminDeleteResult {
  deleted: boolean
  user_id: string
}

/** 列全部用户（仅 admin；不含密码） */
export async function listUsers(): Promise<UserAdminListResult> {
  const response = await apiClient.get<UserAdminListResult>('/ext/user_admin/users')
  return response.data
}

/** 改用户角色（仅 admin；内核侧拒绝对自己操作——防锁死系统） */
export async function updateUserRole(userId: string, role: 'admin' | 'user'): Promise<UserAdminUpdateResult> {
  const response = await apiClient.patch<UserAdminUpdateResult>(
    `/ext/user_admin/users/${userId}/role`,
    { role },
  )
  return response.data
}

/** 改用户归属租户（仅 admin；内核侧拒绝对自己操作） */
export async function updateUserTenant(userId: string, tenantId: string): Promise<UserAdminUpdateResult> {
  const response = await apiClient.patch<UserAdminUpdateResult>(
    `/ext/user_admin/users/${userId}/tenant`,
    { tenant_id: tenantId },
  )
  return response.data
}

/** 删用户（仅 admin；内核侧拒绝删自己——防锁死系统） */
export async function deleteUser(userId: string): Promise<UserAdminDeleteResult> {
  const response = await apiClient.delete<UserAdminDeleteResult>(`/ext/user_admin/users/${userId}`)
  return response.data
}
