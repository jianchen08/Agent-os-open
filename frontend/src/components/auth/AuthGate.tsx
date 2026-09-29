/**
 * 认证闸（ADR 2026-09-28）
 *
 * 未认证访问受保护页时替代原整页跳转 /login：原位渲染不可关闭的登录模态，
 * 登录成功后 ProtectedRoute 直接放行目标页（深链不再经登录页折返 HOME）。
 */

import { LoginModal } from './LoginModal'

/** 认证闸：背景占位 + 不可关闭登录模态 */
export function AuthGate() {
  return (
    <div className="bg-background min-h-screen" data-testid="auth-gate">
      <LoginModal open onClose={() => {}} dismissible={false} />
    </div>
  )
}

export default AuthGate
