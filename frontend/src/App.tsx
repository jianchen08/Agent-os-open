/**
 * 根组件
 *
 * 包含错误边界和路由配置
 */

import { RouterProvider } from 'react-router-dom'
import ErrorBoundary from './components/ErrorBoundary'
import { Toaster } from './components/ui/sonner'
import { createRouter } from './router'

const router = createRouter()

/**
 * 应用根组件
 */
export function App() {
  return (
    <ErrorBoundary>
      <RouterProvider router={router} />
      <Toaster />
    </ErrorBoundary>
  )
}
