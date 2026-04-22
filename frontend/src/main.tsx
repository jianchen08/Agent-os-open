/**
 * 应用入口文件
 *
 * 初始化 React 应用，包括主题系统和认证状态
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { initializeTheme } from './stores/themeStore'
import { useAuthStore } from './stores/authStore'
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

  // 初始化自生长闭环（非阻塞，不阻塞应用启动）
  import('@/services/modules/GrowthLoop').then(({ initializeGrowthLoop }) => {
    initializeGrowthLoop().catch((error) => {
      console.error('自生长闭环初始化失败:', error)
    })
  })

  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>
  )
}

bootstrap().catch((error) => {
  console.error('应用初始化失败:', error)
})
