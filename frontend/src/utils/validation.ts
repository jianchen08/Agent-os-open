/**
 * 表单验证工具（使用 zod）
 *
 * 暴露接口：
 * - usernameSchema: 用户名验证规则
 * - passwordSchema: 密码验证规则
 * - emailSchema: 邮箱验证规则
 * - loginFormSchema: 登录表单验证 Schema
 * - registerFormSchema: 注册表单验证 Schema
 * - messageContentSchema: 消息内容验证 Schema
 * - sessionTitleSchema: 会话标题验证 Schema
 * - validatePasswordStrength(password): 验证密码强度
 * - isBlankString(str): 验证是否为空白字符串
 */

import { z } from 'zod'

export const usernameSchema = z
  .string()
  .min(3, '用户名至少需要3个字符')
  .max(20, '用户名最多20个字符')
  .regex(/^[a-zA-Z0-9_]+$/, '用户名只能包含字母、数字和下划线')

export const passwordSchema = z
  .string()
  .min(8, '密码至少需要8个字符')
  .regex(/[A-Z]/, '密码必须包含至少一个大写字母')
  .regex(/[a-z]/, '密码必须包含至少一个小写字母')
  .regex(/[0-9]/, '密码必须包含至少一个数字')

export const emailSchema = z.string().email('请输入有效的邮箱地址')

export const loginFormSchema = z.object({
  username: usernameSchema,
  password: z.string().min(1, '请输入密码'),
})

export type LoginFormData = z.infer<typeof loginFormSchema>

export const registerFormSchema = z
  .object({
    username: usernameSchema,
    email: z
      .union([
        z.literal(''), // 允许空字符串
        emailSchema, // 或者有效的邮箱格式
      ])
      .optional(),
    password: passwordSchema,
    confirmPassword: z.string().min(1, '请确认密码'),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: '两次输入的密码不一致',
    path: ['confirmPassword'],
  })

export type RegisterFormData = z.infer<typeof registerFormSchema>

export const messageContentSchema = z
  .string()
  .min(1, '消息内容不能为空')
  .max(10000, '消息内容不能超过10000个字符')

export const sessionTitleSchema = z
  .string()
  .min(1, '会话标题不能为空')
  .max(100, '会话标题不能超过100个字符')

export function validatePasswordStrength(password: string): 'weak' | 'medium' | 'strong' {
  let strength = 0

  // 长度检查
  if (password.length >= 8) strength++
  if (password.length >= 12) strength++

  // 复杂度检查
  if (/[a-z]/.test(password)) strength++
  if (/[A-Z]/.test(password)) strength++
  if (/[0-9]/.test(password)) strength++
  if (/[^a-zA-Z0-9]/.test(password)) strength++

  if (strength <= 2) return 'weak'
  if (strength <= 4) return 'medium'
  return 'strong'
}

export function isBlankString(str: string): boolean {
  return str.trim().length === 0
}
