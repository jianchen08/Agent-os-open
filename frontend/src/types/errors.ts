/**
 * 统一错误类型定义
 *
 * 与后端 src/core/errors.py 中的定义保持一致
 */

/**
 * 错误严重程度
 */
export type ErrorSeverity = 'info' | 'warning' | 'error'

/**
 * 错误类别
 */
export type ErrorCategory = 'WS' | 'API' | 'TOOL' | 'DB' | 'MEM' | 'AUTH' | 'VAL' | 'SYS' | 'LLM'

/**
 * 统一错误码定义
 * 格式: CATEGORY_SPECIFIC_CODE
 */
export enum ErrorCode {
  // WebSocket 错误 (1000-1999)
  WS_CONN_1001 = 'WS_CONN_1001', // 连接失败
  WS_CONN_1002 = 'WS_CONN_1002', // 连接超时
  WS_AUTH_1003 = 'WS_AUTH_1003', // 认证失败
  WS_AUTH_1004 = 'WS_AUTH_1004', // 令牌过期
  WS_MSG_1005 = 'WS_MSG_1005', // 消息格式错误
  WS_MSG_1006 = 'WS_MSG_1006', // 消息过大

  // API 错误 (2000-2999)
  API_AUTH_2001 = 'API_AUTH_2001', // 认证失败
  API_PERM_2002 = 'API_PERM_2002', // 权限不足
  API_VAL_2003 = 'API_VAL_2003', // 参数验证失败
  API_NOTF_2004 = 'API_NOTF_2004', // 资源未找到
  API_TIME_2005 = 'API_TIME_2005', // 请求超时

  // 工具错误 (3000-3999)
  TOOL_NOTF_3001 = 'TOOL_NOTF_3001', // 工具不存在
  TOOL_EXEC_3002 = 'TOOL_EXEC_3002', // 工具执行失败
  TOOL_VAL_3003 = 'TOOL_VAL_3003', // 参数验证失败
  TOOL_TIME_3004 = 'TOOL_TIME_3004', // 执行超时
  TOOL_PERM_3005 = 'TOOL_PERM_3005', // 需要审批
  TOOL_EXEC_3006 = 'TOOL_EXEC_3006', // 执行被取消

  // 数据库错误 (4000-4999)
  DB_CONN_4001 = 'DB_CONN_4001', // 连接失败
  DB_EXEC_4002 = 'DB_EXEC_4002', // 执行失败
  DB_TIME_4003 = 'DB_TIME_4003', // 查询超时

  // 记忆错误 (5000-5999)
  MEM_NOTF_5001 = 'MEM_NOTF_5001', // 记忆未找到
  MEM_EXEC_5002 = 'MEM_EXEC_5002', // 检索失败
  MEM_VAL_5003 = 'MEM_VAL_5003', // 格式错误

  // 认证错误 (6000-6999)
  AUTH_VAL_6001 = 'AUTH_VAL_6001', // 凭证无效
  AUTH_TIME_6002 = 'AUTH_TIME_6002', // 令牌过期
  AUTH_FAIL_6003 = 'AUTH_FAIL_6003', // 认证失败

  // 验证错误 (7000-7999)
  VAL_REQ_7001 = 'VAL_REQ_7001', // 缺少必需参数
  VAL_FMT_7002 = 'VAL_FMT_7002', // 格式错误
  VAL_RANGE_7003 = 'VAL_RANGE_7003', // 超出范围

  // 系统错误 (8000-8999)
  SYS_TIME_8001 = 'SYS_TIME_8001', // 系统超时
  SYS_LOAD_8002 = 'SYS_LOAD_8002', // 系统过载
  SYS_ERR_8003 = 'SYS_ERR_8003', // 内部错误

  // LLM 错误 (9000-9999)
  LLM_CONN_9001 = 'LLM_CONN_9001', // 连接失败
  LLM_EXEC_9002 = 'LLM_EXEC_9002', // 调用失败
  LLM_TIME_9003 = 'LLM_TIME_9003', // 调用超时
}

/**
 * 标准错误响应格式
 */
export interface StandardError {
  code: string // 错误码: "TOOL_EXEC_3002"
  message: string // 用户友好消息
  category: ErrorCategory // 错误类别: "TOOL"
  severity: ErrorSeverity // 严重程度: "error"|"warning"|"info"
  timestamp: string // ISO 8601 格式时间
  trace_id: string // 追踪 ID
  path?: string // 请求路径
  details?: Record<string, unknown> // 详细信息(开发环境)
  stack_trace?: string // 堆栈跟踪(开发环境)
  suggested_action?: string // 建议操作
}

/**
 * 错误消息映射
 */
export const ERROR_MESSAGES: Record<string, string> = {
  // WebSocket 错误
  [ErrorCode.WS_CONN_1001]: 'WebSocket 连接失败',
  [ErrorCode.WS_CONN_1002]: 'WebSocket 连接超时',
  [ErrorCode.WS_AUTH_1003]: 'WebSocket 认证失败',
  [ErrorCode.WS_AUTH_1004]: 'WebSocket 令牌已过期',
  [ErrorCode.WS_MSG_1005]: 'WebSocket 消息格式错误',
  [ErrorCode.WS_MSG_1006]: 'WebSocket 消息过大',

  // API 错误
  [ErrorCode.API_AUTH_2001]: 'API 认证失败',
  [ErrorCode.API_PERM_2002]: '权限不足',
  [ErrorCode.API_VAL_2003]: 'API 参数验证失败',
  [ErrorCode.API_NOTF_2004]: '资源未找到',
  [ErrorCode.API_TIME_2005]: 'API 请求超时',

  // 工具错误
  [ErrorCode.TOOL_NOTF_3001]: '工具不存在',
  [ErrorCode.TOOL_EXEC_3002]: '工具执行失败',
  [ErrorCode.TOOL_VAL_3003]: '工具参数验证失败',
  [ErrorCode.TOOL_TIME_3004]: '工具执行超时',
  [ErrorCode.TOOL_PERM_3005]: '工具需要审批',
  [ErrorCode.TOOL_EXEC_3006]: '工具执行被取消',

  // 数据库错误
  [ErrorCode.DB_CONN_4001]: '数据库连接失败',
  [ErrorCode.DB_EXEC_4002]: '数据库执行失败',
  [ErrorCode.DB_TIME_4003]: '数据库查询超时',

  // 记忆错误
  [ErrorCode.MEM_NOTF_5001]: '记忆未找到',
  [ErrorCode.MEM_EXEC_5002]: '记忆检索失败',
  [ErrorCode.MEM_VAL_5003]: '记忆格式错误',

  // 认证错误
  [ErrorCode.AUTH_VAL_6001]: '认证凭证无效',
  [ErrorCode.AUTH_TIME_6002]: '认证令牌已过期',
  [ErrorCode.AUTH_FAIL_6003]: '认证失败',

  // 验证错误
  [ErrorCode.VAL_REQ_7001]: '缺少必需参数',
  [ErrorCode.VAL_FMT_7002]: '格式错误',
  [ErrorCode.VAL_RANGE_7003]: '参数超出允许范围',

  // 系统错误
  [ErrorCode.SYS_TIME_8001]: '系统超时',
  [ErrorCode.SYS_LOAD_8002]: '系统负载过高',
  [ErrorCode.SYS_ERR_8003]: '系统内部错误',

  // LLM 错误
  [ErrorCode.LLM_CONN_9001]: 'LLM 连接失败',
  [ErrorCode.LLM_EXEC_9002]: 'LLM 调用失败',
  [ErrorCode.LLM_TIME_9003]: 'LLM 调用超时',
}

/**
 * 错误严重程度映射
 */
export const ERROR_SEVERITY: Record<string, ErrorSeverity> = {
  // WebSocket 错误
  [ErrorCode.WS_CONN_1001]: 'error',
  [ErrorCode.WS_CONN_1002]: 'warning',
  [ErrorCode.WS_AUTH_1003]: 'error',
  [ErrorCode.WS_AUTH_1004]: 'warning',
  [ErrorCode.WS_MSG_1005]: 'error',
  [ErrorCode.WS_MSG_1006]: 'error',

  // API 错误
  [ErrorCode.API_AUTH_2001]: 'error',
  [ErrorCode.API_PERM_2002]: 'error',
  [ErrorCode.API_VAL_2003]: 'error',
  [ErrorCode.API_NOTF_2004]: 'warning',
  [ErrorCode.API_TIME_2005]: 'warning',

  // 工具错误
  [ErrorCode.TOOL_NOTF_3001]: 'error',
  [ErrorCode.TOOL_EXEC_3002]: 'error',
  [ErrorCode.TOOL_VAL_3003]: 'error',
  [ErrorCode.TOOL_TIME_3004]: 'warning',
  [ErrorCode.TOOL_PERM_3005]: 'info',
  [ErrorCode.TOOL_EXEC_3006]: 'info',

  // 数据库错误
  [ErrorCode.DB_CONN_4001]: 'error',
  [ErrorCode.DB_EXEC_4002]: 'error',
  [ErrorCode.DB_TIME_4003]: 'warning',

  // 记忆错误
  [ErrorCode.MEM_NOTF_5001]: 'warning',
  [ErrorCode.MEM_EXEC_5002]: 'error',
  [ErrorCode.MEM_VAL_5003]: 'error',

  // 认证错误
  [ErrorCode.AUTH_VAL_6001]: 'error',
  [ErrorCode.AUTH_TIME_6002]: 'warning',
  [ErrorCode.AUTH_FAIL_6003]: 'error',

  // 验证错误
  [ErrorCode.VAL_REQ_7001]: 'error',
  [ErrorCode.VAL_FMT_7002]: 'error',
  [ErrorCode.VAL_RANGE_7003]: 'error',

  // 系统错误
  [ErrorCode.SYS_TIME_8001]: 'warning',
  [ErrorCode.SYS_LOAD_8002]: 'warning',
  [ErrorCode.SYS_ERR_8003]: 'error',

  // LLM 错误
  [ErrorCode.LLM_CONN_9001]: 'error',
  [ErrorCode.LLM_EXEC_9002]: 'error',
  [ErrorCode.LLM_TIME_9003]: 'warning',
}

/**
 * 建议操作映射
 */
export const SUGGESTED_ACTIONS: Record<string, string> = {
  // WebSocket 错误
  [ErrorCode.WS_CONN_1001]: '请检查网络连接后重试',
  [ErrorCode.WS_CONN_1002]: '请稍后重试',
  [ErrorCode.WS_AUTH_1003]: '请重新登录',
  [ErrorCode.WS_AUTH_1004]: '正在自动刷新令牌...',
  [ErrorCode.WS_MSG_1005]: '请检查消息格式',
  [ErrorCode.WS_MSG_1006]: '请减少消息内容长度',

  // API 错误
  [ErrorCode.API_AUTH_2001]: '请重新登录',
  [ErrorCode.API_PERM_2002]: '您没有权限执行此操作',
  [ErrorCode.API_VAL_2003]: '请检查参数格式',
  [ErrorCode.API_NOTF_2004]: '请确认资源是否存在',
  [ErrorCode.API_TIME_2005]: '请稍后重试',

  // 工具错误
  [ErrorCode.TOOL_NOTF_3001]: '请确认工具名称是否正确',
  [ErrorCode.TOOL_EXEC_3002]: '请重试或联系管理员',
  [ErrorCode.TOOL_VAL_3003]: '请检查工具参数',
  [ErrorCode.TOOL_TIME_3004]: '请稍后重试',
  [ErrorCode.TOOL_PERM_3005]: '请在审批通过后重试',
  [ErrorCode.TOOL_EXEC_3006]: '工具执行已取消',

  // 数据库错误
  [ErrorCode.DB_CONN_4001]: '请稍后重试或联系管理员',
  [ErrorCode.DB_EXEC_4002]: '请稍后重试',
  [ErrorCode.DB_TIME_4003]: '请稍后重试',

  // 记忆错误
  [ErrorCode.MEM_NOTF_5001]: '请确认记忆是否存在',
  [ErrorCode.MEM_EXEC_5002]: '请重试',
  [ErrorCode.MEM_VAL_5003]: '请检查记忆格式',

  // 认证错误
  [ErrorCode.AUTH_VAL_6001]: '请检查用户名和密码',
  [ErrorCode.AUTH_TIME_6002]: '请重新登录',
  [ErrorCode.AUTH_FAIL_6003]: '请检查认证信息',

  // 验证错误
  [ErrorCode.VAL_REQ_7001]: '请提供所有必需参数',
  [ErrorCode.VAL_FMT_7002]: '请检查参数格式',
  [ErrorCode.VAL_RANGE_7003]: '请检查参数范围',

  // 系统错误
  [ErrorCode.SYS_TIME_8001]: '请稍后重试',
  [ErrorCode.SYS_LOAD_8002]: '请稍后重试',
  [ErrorCode.SYS_ERR_8003]: '请联系管理员',

  // LLM 错误
  [ErrorCode.LLM_CONN_9001]: '请检查 LLM 服务连接',
  [ErrorCode.LLM_EXEC_9002]: '请重试',
  [ErrorCode.LLM_TIME_9003]: '请稍后重试',
}

/**
 * 可重试错误集合
 */
export const RETRYABLE_ERRORS: Set<string> = new Set([
  ErrorCode.WS_CONN_1001,
  ErrorCode.WS_CONN_1002,
  ErrorCode.API_TIME_2005,
  ErrorCode.TOOL_TIME_3004,
  ErrorCode.DB_TIME_4003,
  ErrorCode.SYS_TIME_8001,
  ErrorCode.SYS_LOAD_8002,
  ErrorCode.LLM_TIME_9003,
])

/**
 * 辅助函数：获取错误消息
 */
export function getErrorMessage(errorCode: string): string {
  return ERROR_MESSAGES[errorCode] || '未知错误'
}

/**
 * 辅助函数：获取错误严重程度
 */
export function getErrorSeverity(errorCode: string): ErrorSeverity {
  return ERROR_SEVERITY[errorCode] || 'error'
}

/**
 * 辅助函数：获取建议操作
 */
export function getSuggestedAction(errorCode: string): string | undefined {
  return SUGGESTED_ACTIONS[errorCode]
}

/**
 * 辅助函数：判断是否可重试
 */
export function isRetryableError(errorCode: string): boolean {
  return RETRYABLE_ERRORS.has(errorCode)
}

/**
 * 辅助函数：从错误对象提取错误码
 */
export function extractErrorCode(error: unknown): string | null {
  if (typeof error === 'object' && error !== null) {
    if ('code' in error && typeof error.code === 'string') {
      return error.code
    }
    if ('error_code' in error && typeof error.error_code === 'string') {
      return error.error_code
    }
  }
  return null
}

/**
 * 辅助函数：创建标准错误对象
 */
export function createStandardError(
  errorCode: string,
  traceId?: string,
  details?: Record<string, unknown>,
): StandardError {
  const code = errorCode || ErrorCode.SYS_ERR_8003

  return {
    code,
    message: getErrorMessage(code),
    category: (code.split('_')[0] || 'SYS') as ErrorCategory,
    severity: getErrorSeverity(code),
    timestamp: new Date().toISOString(),
    trace_id: traceId || generateTraceId(),
    details,
    suggested_action: getSuggestedAction(code),
  }
}

/**
 * 辅助函数：生成 trace_id
 */
export function generateTraceId(): string {
  return `${Date.now()}-${Math.random().toString(36).substring(2, 15)}`
}
