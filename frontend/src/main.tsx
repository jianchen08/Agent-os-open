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

  // BUG-FIX-fix_20260507_002: await initializeGrowthLoop 确保模块在渲染前就绪
  // 问题根因: import().then() 不阻塞，React 渲染时模块尚未加载完成导致工作区为空
  // 修复方案: 使用 await 等待 initializeGrowthLoop 完成后再渲染
  if (authStore.isAuthenticated) {
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
