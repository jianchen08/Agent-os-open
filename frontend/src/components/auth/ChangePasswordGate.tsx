/**
 * 首登强制改密闸（D1-4）
 *
 * 播种账号 must_change_password=true 时，ProtectedRoute 以本闸替换路由内容：
 * 未完成改密前无法进入任何受保护页面，改密成功（标记清除）后自动放行。
 */

import { ChangePasswordForm } from './ChangePasswordForm'
import { useAuthStore } from '../../stores/authStore'
import { Button } from '../ui/button'

export function ChangePasswordGate() {
  const logout = useAuthStore((s) => s.logout)

  return (
    <div
      className="bg-background text-foreground flex min-h-screen items-center justify-center px-4 py-12"
      data-testid="change-password-gate"
    >
      <div className="w-full max-w-md space-y-6">
        <div className="space-y-2 text-center">
          <h1 className="text-foreground text-2xl font-bold">请修改初始口令</h1>
          <p className="text-muted-foreground text-sm">
            当前账号仍在使用初始口令。为保障安全，请先设置新口令，修改后即可继续使用。
          </p>
        </div>
        <ChangePasswordForm />
        <div className="pt-2 text-center">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => void logout()}
            data-testid="change-password-logout-button"
          >
            退出登录
          </Button>
        </div>
      </div>
    </div>
  )
}
