/**
 * 用户管理 API 服务
 *
 * 暴露接口：
 * - getUsers(skip, limit): 获取用户列表
 * - getUserStats(): 获取用户统计
 * - createUser(data): 创建用户（含 email 可选字段）
 * - updateUserRole(userId, role): 更新用户角色
 * - updateUserActiveStatus(userId, isActive): 更新用户激活状态
 * - deleteUser(userId): 删除用户
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { reportError } from '@/services/errorReporting'

export interface User {
  id: string
  username: string
  email?: string
  role: 'admin' | 'user'
  is_active: boolean
  created_at: string
  last_login_at?: string
}

export interface UserStats {
  total_users: number
  active_users: number
  admin_count: number
}

export interface CreateUserRequest {
  username: string
  password: string
  email?: string
  role?: 'admin' | 'user'
}

export async function getUsers(skip: number = 0, limit: number = 100): Promise<User[]> {
  try {
    const response = await apiClient.get<User[]>(API_ENDPOINTS.USERS.LIST, {
      params: { skip, limit },
    })
    return response.data
  } catch (error) {
    reportError('获取用户列表失败', 'validation', 'error', {
      code: 'GET_USERS_FAILED',
    })
    throw error
  }
}

export async function getUserStats(): Promise<UserStats> {
  try {
    const response = await apiClient.get<UserStats>(API_ENDPOINTS.USERS.STATS)
    return response.data
  } catch (error) {
    reportError('获取用户统计失败', 'validation', 'error', {
      code: 'GET_STATS_FAILED',
    })
    throw error
  }
}

export async function createUser(data: CreateUserRequest): Promise<User> {
  try {
    const response = await apiClient.post<User>(API_ENDPOINTS.USERS.CREATE, null, {
      params: {
        username: data.username,
        password: data.password,
        email: data.email || '',
        role: data.role || 'user',
      },
    })
    return response.data
  } catch (error) {
    reportError('创建用户失败', 'validation', 'error', {
      code: 'CREATE_USER_FAILED',
    })
    throw error
  }
}

export async function updateUserRole(userId: string, role: 'admin' | 'user'): Promise<User> {
  try {
    const response = await apiClient.put<User>(API_ENDPOINTS.USERS.UPDATE_ROLE(userId), null, {
      params: { role },
    })
    return response.data
  } catch (error) {
    reportError('更新用户角色失败', 'validation', 'error', {
      code: 'UPDATE_ROLE_FAILED',
    })
    throw error
  }
}

export async function updateUserActiveStatus(userId: string, isActive: boolean): Promise<User> {
  try {
    const response = await apiClient.put<User>(API_ENDPOINTS.USERS.UPDATE_ACTIVE(userId), null, {
      params: { is_active: isActive },
    })
    return response.data
  } catch (error) {
    reportError('更新用户状态失败', 'validation', 'error', {
      code: 'UPDATE_STATUS_FAILED',
    })
    throw error
  }
}

export async function deleteUser(userId: string): Promise<{ message: string }> {
  try {
    const response = await apiClient.delete<{ message: string }>(API_ENDPOINTS.USERS.DELETE(userId))
    return response.data
  } catch (error) {
    reportError('删除用户失败', 'validation', 'error', {
      code: 'DELETE_USER_FAILED',
    })
    throw error
  }
}
