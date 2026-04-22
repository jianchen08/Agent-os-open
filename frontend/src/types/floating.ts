/**
 * 悬浮窗相关类型定义
 *
 * 与后端 /api/v1/floating-chat/* 端点对齐
 */

/**
 * 悬浮窗状态类型
 */
export interface FloatingChatStatus {
  /** 是否可用 */
  available: boolean
  /** 可执行文件路径 */
  executable_path?: string
  /** 状态消息 */
  message: string
}

/**
 * 启动请求类型
 */
export interface LaunchRequest {
  /** 会话 ID */
  session_id?: string
  /** 认证 token */
  token?: string
}

/**
 * 启动结果类型
 */
export interface LaunchResult {
  /** 是否成功 */
  success: boolean
  /** 结果消息 */
  message: string
}
