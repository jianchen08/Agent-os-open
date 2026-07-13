/**
 * 工具函数统一导出
 */

// 导出格式化工具
export {
  formatDate,
  formatRelativeTime,
  formatFileSize,
  formatNumber,
} from './format'

// 导出验证工具
export {
  usernameSchema,
  passwordSchema,
  emailSchema,
  loginFormSchema,
  registerFormSchema,
  messageContentSchema,
  sessionTitleSchema,
  validatePasswordStrength,
  isBlankString,
} from './validation'
export type { LoginFormData, RegisterFormData } from './validation'

// 导出存储工具
export { storage, authStorage, uiStorage, STORAGE_KEYS } from './storage'
export type { StorageKey } from './storage'

// 导出数据映射工具
export { mapThreadToSession } from './mappers'

// 导出消息类型判断工具
export { checkIsSystemMessage } from './messageType'
