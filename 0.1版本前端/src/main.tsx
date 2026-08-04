/**
 * 应用入口文件
 *
 * 初始化 React 应用，包括主题系统和认证状态
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { useAuthStore } from './stores/authStore'
import { initializeTheme } from './stores/themeStore'
import { registerGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import { openFile } from '@/services/fileOpener'
import './index.css'

// 禁用浏览器刷新时自动恢复滚动位置：浏览器默认 scrollRestoration='auto'，
// 刷新时会在 DOMContentLoaded 阶段（早于 React 渲染）自动恢复上次的滚动位置，
// 而 MessageList 的 pinToBottom 即使在 useLayoutEffect 同步执行也在浏览器恢复之后，
// 会导致用户看到"先停旧位置再跳底"的中间态。设为 manual 由应用代码完全接管定位。
if ('scrollRestoration' in history) {
  history.scrollRestoration = 'manual'
}

/**
 * 初始化应用
 *
 * 核心策略：先渲染 React（用户看到加载动画），再异步初始化认证。
 * 这样即使后端 API 响应慢或不可用，页面也不会空白。
 * ProtectedRoute 在 isInitializing=true 时会显示加载动画。
 */
async function bootstrap() {
  const root = document.getElementById('root')

  if (!root) {
    throw new Error('找不到根元素 #root')
  }

  await initializeTheme()

  // 注册全局文件打开回调
  registerGlobalOpenFileCallback(async (filePath: string, containerTaskId?: string) => {
    const result = await openFile(filePath, { containerTaskId })
    if (!result.success) {
      console.error('[main] 打开文件失败:', result.message)
    }
  })

  // 先渲染 React 应用，用户立刻看到加载状态而非空白页
  // ProtectedRoute 在 isInitializing=true 时显示加载动画
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )

  // 异步初始化认证状态（不阻塞渲染）
  // initializeAuth 更新 store 后，ProtectedRoute 会自动响应状态变化
  const authStore = useAuthStore.getState()
  await authStore.initializeAuth()

  // initializeAuth() 通过 set() 更新 store，但上面 authStore 是 getState() 的快照，
  // 其 isAuthenticated 仍为 false。这里重新 getState() 获取最新认证状态，
  // 才能正确判断是否初始化 GrowthLoop。
  const freshAuthState = useAuthStore.getState()
  if (freshAuthState.isAuthenticated) {
    try {
      const { initializeGrowthLoop } = await import('@/services/modules/GrowthLoop')
      await initializeGrowthLoop()
    } catch (error) {
      console.error('自生长闭环初始化失败:', error)
    }
  }
}

bootstrap().catch((error) => {
  console.error('应用初始化失败:', error)
})
