/**
 * GlobalInteractionOverlay - 全局交互浮层组件
 *
 * 显示所有待处理的人类交互请求，支持：
 * - 多卡片堆叠展示
 * - 长内容滚动
 * - 最小化为浮动按钮
 * - 全局可见（不依赖当前所在页面）
 */

import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { X, Minimize2, ChevronLeft, ChevronRight } from 'lucide-react'
import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { useInteractionStore } from '@/stores/interactionStore'
import { useSessionStore } from '@/stores/sessionStore'
import { InteractionCard } from './InteractionCard'

export function GlobalInteractionOverlay() {
  const pendingInteractions = useInteractionStore((s) => s.pendingInteractions)
  const isMinimized = useInteractionStore((s) => s.isMinimized)
  const toggleMinimized = useInteractionStore((s) => s.toggleMinimized)
  const dismissInteraction = useInteractionStore((s) => s.dismissInteraction)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)

  const { respondChoice, respondConversation, navigateToTab } =
    useInteractionHandler(activeSessionId)

  const [submittingId, setSubmittingId] = useState<string | null>(null)
  const [currentIndex, setCurrentIndex] = useState(0)

  // 过滤出 pending 状态的交互
  const pendingItems = pendingInteractions.filter((i) => i.status === 'pending')

  // 当前显示的交互
  const currentInteraction = pendingItems[currentIndex] || null

  // 自动重置索引（当交互数量变化时）
  useEffect(() => {
    if (currentIndex >= pendingItems.length) {
      setCurrentIndex(Math.max(0, pendingItems.length - 1))
    }
  }, [pendingItems.length, currentIndex])

  // 响应完成或跳转后自动移除
  useEffect(() => {
    if (!currentInteraction) return
    if (currentInteraction.status === 'responded' || currentInteraction.status === 'navigated') {
      const timer = setTimeout(() => {
        dismissInteraction(currentInteraction.requestId)
      }, 2000)
      return () => clearTimeout(timer)
    }
  }, [currentInteraction, dismissInteraction])

  const handleRespondChoice = useCallback(
    async (optionId: string, optionLabel?: string) => {
      if (!currentInteraction) return
      if (submittingId && submittingId !== currentInteraction.requestId) return
      setSubmittingId(currentInteraction.requestId)
      try {
        await respondChoice(currentInteraction.requestId, optionLabel || optionId)
      } finally {
        setSubmittingId(null)
      }
    },
    [currentInteraction, respondChoice, submittingId],
  )

  const handleRespondText = useCallback(
    async (text: string) => {
      if (!currentInteraction) return
      if (submittingId && submittingId !== currentInteraction.requestId) return
      setSubmittingId(currentInteraction.requestId)
      try {
        await respondConversation(currentInteraction.requestId, text)
      } finally {
        setSubmittingId(null)
      }
    },
    [currentInteraction, respondConversation, submittingId],
  )

  const handleNavigateToTab = useCallback(
    async () => {
      if (!currentInteraction) return
      if (submittingId && submittingId !== currentInteraction.requestId) return
      setSubmittingId(currentInteraction.requestId)
      try {
        await navigateToTab(
          currentInteraction.requestId,
          currentInteraction.pipelineId || currentInteraction.threadId,
          currentInteraction.title,
          (currentInteraction as any).agentLevel,
          currentInteraction.sessionId,
        )
      } finally {
        setSubmittingId(null)
      }
    },
    [currentInteraction, navigateToTab, submittingId],
  )

  const handleDismiss = useCallback(() => {
    if (!currentInteraction) return
    dismissInteraction(currentInteraction.requestId)
  }, [currentInteraction, dismissInteraction])

  const handlePrev = useCallback(() => {
    setCurrentIndex((prev) => (prev > 0 ? prev - 1 : prev))
  }, [])

  const handleNext = useCallback(() => {
    setCurrentIndex((prev) => (prev < pendingItems.length - 1 ? prev + 1 : prev))
  }, [pendingItems.length])

  // ESC 关闭当前交互
  useEffect(() => {
    if (!currentInteraction) return

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        dismissInteraction(currentInteraction.requestId)
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [currentInteraction, dismissInteraction])

  // 最小化状态：显示浮动按钮
  if (isMinimized) {
    if (pendingItems.length === 0) return null

    return createPortal(
      <div
        className="fixed bottom-4 right-4 z-[10000] flex items-center gap-2 rounded-full bg-primary px-4 py-2 text-primary-foreground shadow-lg cursor-pointer hover:bg-primary/90 transition-colors"
        onClick={toggleMinimized}
      >
        <span className="text-sm font-medium">
          {pendingItems.length} 个待处理交互
        </span>
      </div>,
      document.body,
    )
  }

  // 正常状态：显示交互卡片
  if (!currentInteraction) return null

  return createPortal(
    <div className="fixed inset-0 z-[10000] flex items-center justify-center pointer-events-none">
      {/* 背景遮罩（点击关闭） */}
      <div
        className="absolute inset-0 bg-black/30 pointer-events-auto"
        onClick={toggleMinimized}
      />

      {/* 交互卡片容器 */}
      <div className="relative z-10 w-full max-w-2xl mx-4 pointer-events-auto">
        {/* 控制栏 */}
        <div className="flex items-center justify-between mb-2">
          {/* 导航按钮 */}
          <div className="flex items-center gap-2">
            <button
              onClick={handlePrev}
              disabled={currentIndex === 0}
              className="flex h-8 w-8 items-center justify-center rounded-full bg-background/80 backdrop-blur-sm border border-border/50 shadow-sm hover:bg-background disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              title="上一个"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <span className="text-sm text-muted-foreground min-w-[60px] text-center">
              {currentIndex + 1} / {pendingItems.length}
            </span>
            <button
              onClick={handleNext}
              disabled={currentIndex === pendingItems.length - 1}
              className="flex h-8 w-8 items-center justify-center rounded-full bg-background/80 backdrop-blur-sm border border-border/50 shadow-sm hover:bg-background disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              title="下一个"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>

          {/* 操作按钮 */}
          <div className="flex items-center gap-2">
            <button
              onClick={toggleMinimized}
              className="flex h-8 w-8 items-center justify-center rounded-full bg-background/80 backdrop-blur-sm border border-border/50 shadow-sm hover:bg-background transition-colors"
              title="最小化"
            >
              <Minimize2 className="h-4 w-4" />
            </button>
            <button
              onClick={handleDismiss}
              className="flex h-8 w-8 items-center justify-center rounded-full bg-background/80 backdrop-blur-sm border border-border/50 shadow-sm hover:bg-background transition-colors"
              title="关闭"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* 交互卡片 */}
        <div className="max-h-[80vh] overflow-y-auto rounded-lg bg-background shadow-2xl">
          <InteractionCard
            interaction={currentInteraction}
            onRespondChoice={handleRespondChoice}
            onRespondText={handleRespondText}
            onNavigateToTab={handleNavigateToTab}
            onDismiss={handleDismiss}
            isSubmitting={submittingId === currentInteraction.requestId}
          />
        </div>
      </div>
    </div>,
    document.body,
  )
}
