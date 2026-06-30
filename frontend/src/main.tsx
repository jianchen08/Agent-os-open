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

// BUG-FIX-fix_20260630_reload_scroll_restore:
// 浏览器默认 scrollRestoration='auto'，刷新时会在 DOMContentLoaded 阶段
// 自动恢复上次的滚动位置（早于 React 渲染）。MessageList 的 pinToBottom
// 即使在 useLayoutEffect 同步执行，也在浏览器恢复之后 → 用户看到先停在
// "旧位置"，等 React 渲染 + pinToBottom 才跳底 → "先停再跳"中间态。
// 设为 manual 禁用浏览器自动恢复，由应用代码（pinToBottom）完全接管定位。
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
}

bootstrap().catch((error) => {
  console.error('应用初始化失败:', error)
})
