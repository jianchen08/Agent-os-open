
/**
 * 应用入口文件
 *
 * 初始化 React 应用，包括主题系统和认证状态
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { openFile } from '@/services/fileOpener'
import { registerGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import { App } from './App'
import { useAuthStore } from './stores/authStore'
import { initializeTheme } from './stores/themeStore'
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
 * 核心策略：先渲染 React（用户立刻看到页面，不再白屏等待），再异步初始化主题/认证。
 *
 * 为什么先渲染：
 * - initializeTheme()（含 persist rehydrate 兜底 500ms）与 registerWidgets 的
 *   26 个 widget 动态 chunk 均为慢路径，若在 createRoot().render() 之前串行 await，
 *   首屏必须等这些异步工作全部完成才渲染 → 内网/慢环境下面临十几秒白屏。
 * - 主题 CSS 变量有 design-tokens.css :root 默认值兜底（非白屏），且 index.html
 *   内联脚本已按 localStorage 的 theme-storage 提前加 dark/light class，
 *   因此先渲染不会出现主题缺失的白屏，主题在异步初始化完成后无缝刷新。
 * - registerWidgets 仅注册 widget 到 registry，ChatContainer 为懒加载组件，
 *   用户进入聊天页前有充分时间差完成注册，无需阻塞首屏。
 */
async function bootstrap() {
  const root = document.getElementById('root')

  if (!root) {
    throw new Error('找不到根元素 #root')
  }

  // 先渲染 React 应用，用户立刻看到加载状态而非空白页
  // ProtectedRoute 在 isInitializing=true 时显示加载动画
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )

  // 异步初始化主题（不阻塞首屏渲染；:root 默认变量兜底，不会白屏）
  try {
    await initializeTheme()
  } catch (error) {
    console.error('主题初始化失败:', error)
  }

  // 预注册工作区面板 widget（顶栏可打开设置/监控等，不依赖登录）
  // 放在渲染之后异步执行：注册 26 个 widget 组件 chunk 不再阻塞首屏
  try {
    const { initializeWidgets } = await import('@/services/schema/registerWidgets')
    initializeWidgets()
  } catch (error) {
    console.error('Widget 预注册失败:', error)
  }

  // 注册全局文件打开回调
  registerGlobalOpenFileCallback(async (filePath: string, containerTaskId?: string) => {
    const result = await openFile(filePath, { containerTaskId })
    if (!result.success) {
      console.error('[main] 打开文件失败:', result.message)
    }
  })

  // 异步初始化认证状态（不阻塞渲染）
  // initializeAuth 更新 store 后，ProtectedRoute 会自动响应状态变化
  const authStore = useAuthStore.getState()
  await authStore.initializeAuth()

  // initializeAuth() 通过 set() 更新 store，但上面 authStore 是 getState() 的快照，
  // 其 isAuthenticated 仍为 false。这里重新 getState() 获取最新认证状态，
  // 才能正确判断是否初始化 GrowthLoop。
  const freshAuthState = useAuthStore.getState()
  // DEV 无登录也要能看到壳层页签；已登录再走完整 GrowthLoop
  if (freshAuthState.isAuthenticated || import.meta.env.DEV) {
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
