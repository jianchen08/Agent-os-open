/**
 * 路由守卫与懒加载路由装配（自 router.tsx 抽出，行为零变化）。
 *
 * 独立成模块的理由：装配件的测试（LazyRoute 边界行为）不应拖入 router.tsx
 * 的全量静态依赖图（40+ 模块，含 WS 单例/查询钩子等含异步副作用的模块）——
 * 全量车道下worker回收竞态使邻接文件拿到半死环境（EnvironmentTeardownError
 * 实证：pending console 关闭竞态），后续文件事件失效。
 */
import { Suspense, type ReactNode } from 'react'
import ErrorBoundary from '@/components/ErrorBoundary'
import { AuthGate } from '@/components/auth/AuthGate'
import { ChangePasswordGate } from '@/components/auth/ChangePasswordGate'
import { GlobalInteractionOverlay } from '@/components/chat/GlobalInteractionOverlay'
import { SchemaFullscreenHost } from '@/components/schema/SchemaFullscreenHost'
import { useAuthStore } from '@/stores/authStore'

/** 懒加载 fallback */
const LazyFallback = <div className="text-muted-foreground p-4">加载中...</div>

/**
 * 懒加载路由元素统一装配：ProtectedRoute → 路由级 ErrorBoundary → Suspense(lazy)。
 *
 * 路由级边界（E11）：懒加载 chunk 拉取失败（部署后旧 chunk 404/网络抖动）或
 * 路由组件渲染抛错时，降级 UI 只替换该路由内容，认证壳保持存活——若漏到
 * App.tsx 顶层边界，整个 RouterProvider 被卸载，导航彻底瘫痪只能整页刷新。
 */
export function LazyRoute({ children }: { children: ReactNode }): ReactNode {
  return (
    <ProtectedRoute>
      <ErrorBoundary>
        <Suspense fallback={LazyFallback}>{children}</Suspense>
      </ErrorBoundary>
    </ProtectedRoute>
  )
}

/** 路由守卫组件 检查用户认证状态： */
export function ProtectedRoute({ children }: { children: ReactNode }): ReactNode {
  const { isAuthenticated, isInitializing, mustChangePassword } = useAuthStore()

  // 开发与生产行为一致（2026-09-13 用户裁定）：无 dev 放行旁路——旁路会让
  // 「未登录却见完整主界面」只在开发可见，等价于把认证回归挡在生产首日。

  // 首登强制改密闸（D1-4）：播种账号未改密前拦下所有受保护页面
  if (isAuthenticated && mustChangePassword) {
    return <ChangePasswordGate />
  }

  if (isInitializing) {
    return (
      <div className="bg-background text-foreground flex min-h-screen items-center justify-center">
        <div className="space-y-2 text-center">
          <div className="border-primary mx-auto h-8 w-8 animate-spin rounded-full border-2 border-t-transparent" />
          <p className="text-muted-foreground text-sm">加载中...</p>
        </div>
      </div>
    )
  }

  if (!isAuthenticated) {
    return <AuthGate />
  }

  return (
    <>
      {children}
      {/* 全局交互浮层：在所有受保护页面中显示待处理交互 */}
      <GlobalInteractionOverlay />
      {/* 全屏声明浮层：订阅插件 ui_schema 声明的事件（on_event:*），渲染 fullscreen 空间 widget */}
      <SchemaFullscreenHost />
    </>
  )
}
