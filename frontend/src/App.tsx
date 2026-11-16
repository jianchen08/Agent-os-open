/**
 * 根组件
 *
 * 包含错误边界和路由配置
 */

import { PersistQueryClientProvider } from '@tanstack/react-query-persist-client'
import { useEffect } from 'react'
import { RouterProvider } from 'react-router-dom'
import ErrorBoundary from './components/ErrorBoundary'
import { ExtensionHost } from './components/extension/ExtensionHost'
import { usePluginHealthNotifications } from './components/layout/PluginHealthNotifications'
import { TitleBar } from './components/layout/TitleBar'
import { useUpdateAvailableNotification } from './components/layout/UpdateAvailableNotification'
import { SkinConsentDialog } from './components/shared/SkinConsentDialog'
import { Toaster } from './components/ui/sonner'
import { createRouter } from './router'
import { queryClient } from './services/query/queryClient'
import { queryPersister } from './services/query/queryPersister'
import { installImeFocusScroll } from './utils/imeFocusScroll'
import { installNotificationNavigateListener } from './utils/systemNotification'

const router = createRouter()

/**
 * 应用根组件
 */
export function App() {
  // 插件不可用降级 → 通知中心条目 + 恢复对账轮询（原顶栏横幅并入通知面）
  usePluginHealthNotifications()

  // 新版本可用 → 通知中心条目（P-24，不做自动更新；仅提示版本与下载地址。
  // Electron 专属；Web/旧版壳下 electronAPI.update 缺失为 no-op）
  useUpdateAvailableNotification()

  // 壳层系统通知点击 → 跳转对应对话（Electron 专属；Web 无桥接为 no-op）。
  // 卸载函数随组件卸载执行，挂载一次订阅一次。
  useEffect(() => installNotificationNavigateListener(), [])

  // 移动端 IME 对焦滚动：键盘弹出时任意输入框滚到键盘上沿之上（移动视口门控，
  // 桌面零动作），与主聊天输入框钉底可见同一效果的全局兜底。
  useEffect(() => installImeFocusScroll(), [])

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
        {/* 皮肤 hooks 启用确认卡（2026-09-25 准入分级：宿主 JS 执行须用户点头） */}
        <SkinConsentDialog />
        <Toaster />
      </PersistQueryClientProvider>
    </ErrorBoundary>
  )
}
