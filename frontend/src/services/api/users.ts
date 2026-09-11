/**
 * 用户域 API 服务
 *
 * 暴露接口：
 * - getUsers(skip, limit): 获取用户列表（调试中心用户子页消费）
 *
 * 用户管理页（统计/启停/删除）已 widget 化为 user_admin 插件声明页
 * （/admin widget_stage 组台，行操作直连 /ext/user_admin 端点），
 * 前端不再持有该域的交互函数。
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { reportError, ErrorSeverity, ErrorType } from '@/services/errorReporting'

export interface User {
  id: string
  username: string
  email?: string
  role: 'admin' | 'user'
  is_active: boolean
  created_at: string
  last_login_at?: string
}

export async function getUsers(skip: number = 0, limit: number = 100): Promise<User[]> {
  try {
    const response = await apiClient.get<User[]>(API_ENDPOINTS.USERS.LIST, {
      params: { skip, limit },
    })
    return response.data
  } catch (error) {
    reportError('获取用户列表失败', {
      type: ErrorType.VALIDATION,
      severity: ErrorSeverity.ERROR,
      code: 'GET_USERS_FAILED',
        })
    throw error
  }
}
