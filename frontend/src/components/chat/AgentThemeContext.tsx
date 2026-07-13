/**
 * Agent 主题上下文
 *
 * 提供 Agent 级别的主题定制功能
 */

import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { useAgentStore } from '@/stores/agentStore'
import { getAgentTheme, type AgentTheme } from '@/config/agentThemes'

/**
 * 扩展的 Agent 主题接口
 */
interface ExtendedAgentTheme extends AgentTheme {
  /** 主题显示名称 */
  displayName?: string
  /** 消息气泡样式 */
  messageBubble?: {
    backgroundColor?: string
    textColor?: string
    borderColor?: string
    borderRadius?: string
    padding?: string
    boxShadow?: string
  }
  /** 布局配置 */
  layout?: {
    messageSpacing?: string
    bubbleMaxWidth?: string
    avatarSize?: string
  }
  /** 字体配置 */
  typography?: {
    fontFamily?: string
    fontSize?: string
    lineHeight?: string
  }
  /** 背景配置 */
  background?: {
    value?: string
    opacity?: number
  }
}

/**
 * Agent 主题上下文值
 */
interface AgentThemeContextValue {
  /** 当前 Agent 主题配置 */
  agentTheme: ExtendedAgentTheme | null
  /** 应用 Agent 主题到 DOM 元素 */
  applyAgentTheme: (element: HTMLElement) => void
  /** 获取 Agent 显示名称 */
  getDisplayName: () => string
  /** 获取 Agent 头像配置 */
  getAvatarConfig: () => ExtendedAgentTheme['avatar']
}

/** 默认上下文值 */
const defaultValue: AgentThemeContextValue = {
  agentTheme: null,
  applyAgentTheme: () => {},
  getDisplayName: () => '',
  getAvatarConfig: () => undefined,
}

const AgentThemeContext = createContext<AgentThemeContextValue>(defaultValue)

/**
 * Agent 主题提供者属性
 */
export interface AgentThemeProviderProps {
  /** Agent ID */
  agentId: string | null
  /** 子组件 */
  children: ReactNode
}

/**
 * Agent 主题提供者
 */
export function AgentThemeProvider({ agentId, children }: AgentThemeProviderProps) {
  const agents = useAgentStore((state) => state.agents)

  const agentTheme = useMemo((): ExtendedAgentTheme | null => {
    if (!agentId) return null
    const agent = agents.find((a) => a.id === agentId)
    if (!agent) return null

    const isMainAgent = agent.configId === 'lingxi' || agent.type === 'system'

    const baseTheme = getAgentTheme(isMainAgent)

    return {
      ...baseTheme,
      displayName: agent.name,
      messageBubble: {
        backgroundColor: baseTheme.colors.background,
        textColor: baseTheme.colors.text,
        borderColor: baseTheme.colors.border,
        borderRadius: baseTheme.bubble.borderRadius,
        boxShadow: baseTheme.bubble.boxShadow,
      },
    }
  }, [agentId, agents])

  const getDisplayName = () => {
    if (!agentId) return ''
    const agent = agents.find((a) => a.id === agentId)
    return agentTheme?.displayName || agent?.name || ''
  }

  const getAvatarConfig = () => {
    return agentTheme?.avatar
  }

  /** 应用 Agent 主题到 DOM 元素 */
  const applyAgentTheme = (_element: HTMLElement) => {
    if (!agentTheme) return

    const root = document.documentElement

    const agentVars = [
      '--agent-bubble-bg',
      '--agent-bubble-text',
      '--agent-bubble-border',
      '--agent-bubble-radius',
      '--agent-bubble-padding',
      '--agent-bubble-shadow',
      '--agent-message-spacing',
      '--agent-bubble-max-width',
      '--agent-avatar-size',
      '--agent-font-family',
      '--agent-font-size',
      '--agent-line-height',
      '--agent-background',
      '--agent-background-opacity',
    ]
    agentVars.forEach((v) => root.style.removeProperty(v))

    if (agentTheme.messageBubble) {
      const bubble = agentTheme.messageBubble
      if (bubble.backgroundColor)
        root.style.setProperty('--agent-bubble-bg', bubble.backgroundColor)
      if (bubble.textColor) root.style.setProperty('--agent-bubble-text', bubble.textColor)
      if (bubble.borderColor) root.style.setProperty('--agent-bubble-border', bubble.borderColor)
      if (bubble.borderRadius) root.style.setProperty('--agent-bubble-radius', bubble.borderRadius)
      if (bubble.padding) root.style.setProperty('--agent-bubble-padding', bubble.padding)
      if (bubble.boxShadow) root.style.setProperty('--agent-bubble-shadow', bubble.boxShadow)
    }

    if (agentTheme.layout) {
      const layout = agentTheme.layout
      if (layout.messageSpacing)
        root.style.setProperty('--agent-message-spacing', layout.messageSpacing)
      if (layout.bubbleMaxWidth)
        root.style.setProperty('--agent-bubble-max-width', layout.bubbleMaxWidth)
      if (layout.avatarSize) root.style.setProperty('--agent-avatar-size', layout.avatarSize)
    }

    if (agentTheme.typography) {
      const typo = agentTheme.typography
      if (typo.fontFamily) root.style.setProperty('--agent-font-family', typo.fontFamily)
      if (typo.fontSize) root.style.setProperty('--agent-font-size', typo.fontSize)
      if (typo.lineHeight) root.style.setProperty('--agent-line-height', typo.lineHeight)
    }

    if (agentTheme.background) {
      const bg = agentTheme.background
      if (bg.value) root.style.setProperty('--agent-background', bg.value)
      if (bg.opacity !== undefined)
        root.style.setProperty('--agent-background-opacity', String(bg.opacity))
    }
  }

  const contextValue = useMemo(
    () => ({
      agentTheme,
      applyAgentTheme,
      getDisplayName,
      getAvatarConfig,
    }),
    [agentTheme, agents, agentId],
  )

  return <AgentThemeContext.Provider value={contextValue}>{children}</AgentThemeContext.Provider>
}

/**
 * 使用 Agent 主题
 */
export function useAgentTheme(): AgentThemeContextValue {
  const context = useContext(AgentThemeContext)
  return context || defaultValue
}
