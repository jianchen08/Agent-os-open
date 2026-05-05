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

  // BUG-FIX-fix_20260506_001: 仅在已认证时初始化自生长闭环
  // 问题根因: 无条件启动轮询导致未认证时 401 死循环
  // 修复方案: 检查 isAuthenticated 状态，仅在已认证时启动
  if (authStore.isAuthenticated) {
    import('@/services/modules/GrowthLoop').then(({ initializeGrowthLoop }) => {
      initializeGrowthLoop().catch((error) => {
        console.error('自生长闭环初始化失败:', error)
      })
    })
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
