/**
 * 客户端能力声明
 *
 * 启动时向后端注册客户端的渲染能力
 * 后端根据能力返回过滤后的 Schema
 */

import { registerClientCapabilities } from '@/services/api/modules'
import { loggers } from '@/utils/logger'

/** 客户端能力配置 */
interface ClientCapabilityConfig {
  renderingSpaces: string[]
  supportedWidgets: string[]
  clientType: string
  version: string
}

/**
 * 检测当前客户端的渲染能力
 */
function detectCapabilities(): ClientCapabilityConfig {
  const width = typeof window !== 'undefined' ? window.innerWidth : 1280
  const isMobile = width < 768

  const desktopSpaces = ['chat', 'workspace', 'floating', 'dock', 'fullscreen']
  const mobileSpaces = ['chat', 'floating']

  const desktopWidgets = [
    'form',
    'chart',
    'gallery',
    'table',
    'progress',
    'code_block',
    'status_card',
    'decision',
    'kanban',
    'editor',
    'terminal',
    'file_tree',
  ]
  const mobileWidgets = [
    'form',
    'chart',
    'gallery',
    'table',
    'progress',
    'code_block',
    'status_card',
    'decision',
  ]

  return {
    renderingSpaces: isMobile ? mobileSpaces : desktopSpaces,
    supportedWidgets: isMobile ? mobileWidgets : desktopWidgets,
    clientType: isMobile ? 'mobile' : 'desktop',
    version: '1.0.0',
  }
}

/**
 * 注册客户端能力
 */
export async function registerCapabilities(): Promise<void> {
  const capabilities = detectCapabilities()

  try {
    await registerClientCapabilities(capabilities)
    loggers.websocket.info('客户端能力注册成功:', capabilities.clientType)
  } catch (error) {
    loggers.websocket.warn('客户端能力注册失败:', error)
  }
}
