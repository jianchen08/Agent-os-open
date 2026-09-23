/**
 * 根组件
 *
 * 包含错误边界和路由配置
 */

import { PersistQueryClientProvider } from '@tanstack/react-query-persist-client'
import { RouterProvider } from 'react-router-dom'
import ErrorBoundary from './components/ErrorBoundary'
import { ExtensionHost } from './components/extension/ExtensionHost'
import { TitleBar } from './components/layout/TitleBar'
import { Toaster } from './components/ui/sonner'
import { createRouter } from './router'
import { queryClient } from './services/query/queryClient'
import { queryPersister } from './services/query/queryPersister'

const router = createRouter()

/**
 * 应用根组件
 */
export function App() {
  return (
    <ErrorBoundary>
      <PersistQueryClientProvider
        client={queryClient}
        persistOptions={{
          persister: queryPersister,
          // 缓存最长保留 24h；buster 变更可整体作废旧缓存
          maxAge: 24 * 60 * 60 * 1000,
          buster: 'v1',
        }}
      >
        {/* Electron 主窗口自定义标题栏（组件内自门控：Web / 子浮窗渲染空）。
            全局挂载以覆盖登录页；主界面顶带经 auth-session 同尺寸垫声明其挖洞 */}
        <TitleBar />
        <RouterProvider router={router} />
        <ExtensionHost />
        <Toaster />
      </PersistQueryClientProvider>
    </ErrorBoundary>
  )
}
