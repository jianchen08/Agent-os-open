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
import './index.css'

/**
 * 初始化应用
 */
async function bootstrap() {
  const root = document.getElementById('root')

  if (!root) {
    throw new Error('找不到根元素 #root')
  }

  await initializeTheme()

  const authStore = useAuthStore.getState()
  await authStore.initializeAuth()

  // BUG-FIX-fix_20260507_003: 修复刷新后 GrowthLoop 不初始化的问题
  // 问题根因: getState() 返回的是快照，initializeAuth() 通过 set() 更新 store 后，
  //          旧快照的 isAuthenticated 仍为 false，导致 initializeGrowthLoop 永远不执行
  // 修复方案: initializeAuth 完成后重新 getState() 获取最新认证状态
  const freshAuthState = useAuthStore.getState()
  if (freshAuthState.isAuthenticated) {
    try {
      const { initializeGrowthLoop } = await import('@/services/modules/GrowthLoop')
      await initializeGrowthLoop()
    } catch (error) {
      console.error('自生长闭环初始化失败:', error)
    }
  }

  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

bootstrap().catch((error) => {
  console.error('应用初始化失败:', error)
})
