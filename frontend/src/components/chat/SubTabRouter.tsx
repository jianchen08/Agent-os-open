/**
 * SubTabRouter - 子Tab路由增强组件
 *
 * 功能：
 * - 流式消息路由守卫（确保 pipeline_id 正确映射到 Tab）
 * - 输入消息路由守卫（parentRecordId 注入验证）
 * - 路由失败 fallback（消息显示在主 Tab 并提示）
 * - Tab 切换时的消息缓冲区（防止切换丢失）
 *
 * 本组件为无 UI 的逻辑层组件，挂载在 ChatContainer 内部，
 * 通过 hooks 与 agentTabStore / streamingStore 协同工作。
 */

import { useCallback, useEffect, useRef } from 'react'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useNotificationStore } from '@/stores/notificationStore'

export interface SubTabRouterProps {
  /** 当前会话 ID */
  sessionId: string
}

/** 消息缓冲区条目 */
interface BufferedMessage {
  tabId: string
  message: any
  timestamp: number
}

/**
 * SubTabRouter 组件
 *
 * 不渲染任何 UI，仅作为逻辑层挂载点。
 * 负责：
 * 1. 监听 pipeline_id 映射变化，确保流式消息路由到正确 Tab
 * 2. 为子 Tab 输入注入 parentRecordId 验证
 * 3. 路由失败时 fallback 到主 Tab
 * 4. Tab 切换时缓冲消息，防止丢失
 */
export function SubTabRouter({ sessionId: _sessionId }: SubTabRouterProps) {
  /** 消息缓冲区 */
  const messageBuffer = useRef<BufferedMessage[]>([])
  /** 已报告的路由失败（防止重复通知） */
  const reportedFailures = useRef<Set<string>>(new Set())

  const tabs = useAgentTabStore((s) => s.tabs)
  const pipelineTabMap = useAgentTabStore((s) => s.pipelineTabMap)
  const addMessageToTab = useAgentTabStore((s) => s.addMessageToTab)
  const registerPipelineTab = useAgentTabStore((s) => s.registerPipelineTab)
  const getTabIdByPipeline = useAgentTabStore((s) => s.getTabIdByPipeline)

  const addNotification = useNotificationStore((s) => s.addNotification)

  /** 已注册的 pipeline 集合，避免重复注册导致循环 */
  const registeredPipelines = useRef<Set<string>>(new Set())

  /**
   * 流式消息路由守卫
   *
   * 监听 pipelineTabMap 变化，验证所有 Tab 都有有效的映射。
   * 发现孤儿 Tab（有 parentRecordId 但无 pipeline 映射）时发出警告。
   *
   * 依赖数组刻意不含 pipelineTabMap（仅含 tabs 等）：effect 内调用 registerPipelineTab
   * 会修改 pipelineTabMap，若它也在依赖数组中会形成 effect → 修改依赖 → 重触发 → 无限循环。
   * 改用 useRef（registeredPipelines）跟踪已注册的 pipeline，避免重复注册，打破循环。
   */
  useEffect(() => {
    for (const tab of tabs) {
      if (tab.agentLevel !== 1 && tab.parentRecordId && tab.pipelineRunId) {
        if (!registeredPipelines.current.has(tab.pipelineRunId)) {
          const mappedTabId = getTabIdByPipeline(tab.pipelineRunId)
          if (!mappedTabId) {
            registeredPipelines.current.add(tab.pipelineRunId)
            registerPipelineTab(tab.pipelineRunId, tab.id)
          }
        }
      }
    }
  }, [tabs, getTabIdByPipeline, registerPipelineTab])

  /**
   * 消息缓冲区：Tab 切换时暂存消息
   *
   * 当 targetTabId 对应的 Tab 暂时不存在时（Tab 正在创建中），
   * 将消息缓冲，等 Tab 创建后再写入。
   */
  const bufferMessage = useCallback((tabId: string, message: any) => {
    messageBuffer.current.push({
      tabId,
      message,
      timestamp: Date.now(),
    })

    // 清理超过 30 秒的缓冲消息
    const now = Date.now()
    messageBuffer.current = messageBuffer.current.filter(
      (entry) => now - entry.timestamp < 30_000,
    )
  }, [])

  /**
   * 路由消息到目标 Tab
   *
   * 路由优先级：
   * 1. 目标 Tab 存在 → 直接写入
   * 2. 目标 Tab 不存在，缓冲中 → 写入缓冲区
   * 3. 目标 Tab 不存在，超过重试 → fallback 到主 Tab + 通知
   */
  const routeMessageToTab = useCallback(
    (tabId: string, message: any) => {
      const tabExists = tabs.some((t) => t.id === tabId)

      if (tabExists) {
        addMessageToTab(tabId, message)

        // 顺便刷新缓冲区中该 Tab 的消息
        const buffered = messageBuffer.current.filter((b) => b.tabId === tabId)
        for (const entry of buffered) {
          addMessageToTab(tabId, entry.message)
        }
        messageBuffer.current = messageBuffer.current.filter((b) => b.tabId !== tabId)
      } else {
        // Tab 尚未创建，缓冲消息
        bufferMessage(tabId, message)
      }
    },
    [tabs, addMessageToTab, bufferMessage],
  )

  /**
   * 路由失败 fallback
   *
   * 如果消息无法路由到目标 Tab（Tab 不存在或已关闭），
   * 仅通知用户路由失败，不写入主 Tab——否则子管道消息会污染主管道，
   * 且主管道可能已有该消息（通过其他路径）造成重复渲染。
   */
  const fallbackToMainTab = useCallback(
    (message: any, targetTabId: string) => {
      const failureKey = `${targetTabId}-${message.id ?? 'unknown'}`
      if (reportedFailures.current.has(failureKey)) return

      reportedFailures.current.add(failureKey)

      // 发出路由失败通知，不再写入主 Tab（避免跨管道污染和重复）
      addNotification({
        category: 'alert',
        title: '消息路由失败',
        message: `一条消息未能路由到目标标签页 (${targetTabId})，请刷新页面或切换到该标签查看。`,
        priority: 'high',
        isBlocking: false,
        autoDismissMs: 8000,
        sourceId: targetTabId,
      })
    },
    [addNotification],
  )

  /**
   * 清理过期的路由失败记录
   */
  useEffect(() => {
    const interval = setInterval(() => {
      reportedFailures.current.clear()
    }, 60_000)
    return () => clearInterval(interval)
  }, [])

  /**
   * 输入路由守卫
   *
   * 验证子 Tab 输入消息的 parentRecordId 是否有效。
   * 无效时回退到主管道发送。
   */
  const validateInputRouting = useCallback(
    (parentRecordId: string | undefined, targetTabId: string | undefined) => {
      if (!parentRecordId || !targetTabId) return { valid: true }

      const tab = tabs.find((t) => t.id === targetTabId)
      if (!tab) {
        addNotification({
          category: 'alert',
          title: '输入路由异常',
          message: '目标标签页不存在，消息将发送到主管道。',
          priority: 'normal',
          isBlocking: false,
          autoDismissMs: 5000,
        })
        return { valid: false }
      }

      if (tab.parentRecordId !== parentRecordId) {
        addNotification({
          category: 'alert',
          title: '输入路由不匹配',
          message: '标签页的 parentRecordId 与输入不匹配，消息将发送到主管道。',
          priority: 'high',
          isBlocking: false,
          autoDismissMs: 5000,
        })
        return { valid: false }
      }

      return { valid: true }
    },
    [tabs, addNotification],
  )

  // 暴露路由方法到全局（供 ChatContainer 等外部组件调用）
  // 使用 window 属性挂载，避免 prop drilling
  useEffect(() => {
    const routerApi = {
      routeMessageToTab,
      fallbackToMainTab,
      validateInputRouting,
    }
    ;(window as any).__subTabRouter = routerApi
    return () => {
      delete (window as any).__subTabRouter
    }
  }, [routeMessageToTab, fallbackToMainTab, validateInputRouting])

  // 无 UI 渲染
  return null
}

/**
 * 获取 SubTabRouter 实例的路由 API
 * 供 ChatContainer 等外部组件调用
 */
export function getSubTabRouterApi() {
  return (window as any).__subTabRouter as {
    routeMessageToTab: (tabId: string, message: any) => void
    fallbackToMainTab: (message: any, targetTabId: string) => void
    validateInputRouting: (
      parentRecordId: string | undefined,
      targetTabId: string | undefined,
    ) => { valid: boolean }
  } | null
}
