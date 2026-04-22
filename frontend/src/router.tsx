/**
 * 路由配置
 *
 * 定义应用的所有路由，包含登录/注册和主页面
 */

import { createBrowserRouter, Navigate } from 'react-router-dom'
import { lazy, Suspense } from 'react'
import type { ReactNode } from 'react'
import { ROUTES } from './constants/routes'
import { LoginPage } from './pages/auth/LoginPage'
import { RegisterPage } from './pages/auth/RegisterPage'

const ModulesSettingsPage = lazy(() =>
  import('@/pages/settings/ModulesSettingsPage').then(m => ({ default: m.ModulesSettingsPage }))
)

/**
 * 占位主页面组件（Phase 0 骨架）
 */
function HomePage(): ReactNode {
  return (
    <div className="min-h-screen flex items-center justify-center bg-background">
      <div className="text-center space-y-4">
        <h1 className="text-4xl font-bold text-foreground">
          超级终端
        </h1>
        <p className="text-lg text-muted-foreground">
          Phase 0 — 项目初始化完成
        </p>
        <div className="flex gap-4 justify-center mt-8">
          <a
            href={ROUTES.LOGIN}
            className="px-6 py-2 bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity"
          >
            登录
          </a>
          <a
            href={ROUTES.REGISTER}
            className="px-6 py-2 bg-secondary text-secondary-foreground rounded-lg hover:opacity-90 transition-opacity"
          >
            注册
          </a>
        </div>
      </div>
    </div>
  )
}

/**
 * 创建路由器实例
 */
export function createRouter() {
  return createBrowserRouter([
    {
      path: ROUTES.HOME,
      element: <HomePage />,
    },
    {
      path: '/settings/modules',
      element: (
        <Suspense fallback={<div className="p-4 text-muted-foreground">加载中...</div>}>
          <ModulesSettingsPage />
        </Suspense>
      ),
    },
    {
      path: ROUTES.LOGIN,
      element: <LoginPage />,
    },
    {
      path: ROUTES.REGISTER,
      element: <RegisterPage />,
    },
    {
      path: '*',
      element: <Navigate to={ROUTES.HOME} replace />,
    },
  ])
}
