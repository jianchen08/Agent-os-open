/**
 * GlobalInteractionOverlay - 全局交互浮层组件
 *
 * 显示所有待处理的人类交互请求，支持：
 * - 多卡片堆叠展示
 * - 长内容滚动
 * - 最小化为浮动徽标（含审批等待倒计时）
 * - 全局可见（不依赖当前所在页面）
 * - 右下角悬浮、锚定 composer 上缘（BUG-43）：composer 全宽横贯底部
 *   （ChatContainer px-3 + ChatInput w-full），浮层实测其几何抬升锚定，
 *   不拦截输入区任何指针；无 composer 的页面回退贴底右下。宽度有界
 *   （min(26rem, 视口-2rem)），窄视口不横向溢出。
 */

import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { X, Minimize2, ChevronLeft, ChevronRight } from '@/assets/icons'
import { toast } from '@/components/ui/sonner'
import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { useInteractionStore } from '@/stores/interactionStore'
import { useSessionStore } from '@/stores/sessionStore'
import { approvalDeadlineMs, formatRemaining } from '@/utils/approvalCountdown'
import { logger } from '@/utils/logger'
import { InteractionCard } from './InteractionCard'
import type { PendingInteraction } from '@/stores/interactionStore'

/** 浮层与 composer 上缘的间距（px） */
const COMPOSER_GAP_PX = 8
const COMPOSER_SELECTOR = '[data-testid="chat-composer"]'

/**
 * composer 避让高度（浮层 bottom 取值）：浮层贴底必压全宽输入区右侧
 * （含发送按钮）——实测 composer 顶缘抬升浮层，随输入增高（ResizeObserver）
 * 与路由挂卸（MutationObserver）自适应；无 composer 或不在视口内
 * （top<=0，含零矩形）回退 0（贴底）。
 */
function useComposerClearance(): number {
  const [clearance, setClearance] = useState(0)

  useEffect(() => {
    let watched: Element | null = null
    let resizeObserver: ResizeObserver | null = null

    const measure = (el: Element) => {
      const top = el.getBoundingClientRect().top
      const next = top > 0 ? Math.ceil(window.innerHeight - top) + COMPOSER_GAP_PX : 0
      setClearance((prev) => (prev === next ? prev : next))
    }
    const watch = (el: Element) => {
      resizeObserver?.disconnect()
      resizeObserver = new ResizeObserver(() => measure(el))
      resizeObserver.observe(el)
      watched = el
      measure(el)
    }
    const unwatch = () => {
      resizeObserver?.disconnect()
      resizeObserver = null
      watched = null
      setClearance(0)
    }

    // 浮层常驻路由外层，composer 随页面挂载/卸载：DOM 变更时跟随换绑
    const mutationObserver = new MutationObserver(() => {
      const found = document.querySelector(COMPOSER_SELECTOR)
      if (found === watched) return
      if (found) watch(found)
      else unwatch()
    })
    mutationObserver.observe(document.body, { childList: true, subtree: true })

    const initial = document.querySelector(COMPOSER_SELECTOR)
    if (initial) watch(initial)

    const onResize = () => {
      if (watched) measure(watched)
    }
    window.addEventListener('resize', onResize)
    return () => {
      mutationObserver.disconnect()
      resizeObserver?.disconnect()
      window.removeEventListener('resize', onResize)
    }
  }, [])

  return clearance
}

/** 收起徽标文案：审批等待取最近到期的剩余时间，无时限交互保持计数文案 */
function useMinimizedBadgeText(items: PendingInteraction[]): string {
  // 与卡内倒计时同门禁：仅有时限等待（timeoutSeconds>0）的交互参与倒计时
  const deadlines = items
    .filter((item) => (item.timeoutSeconds ?? 0) > 0)
    .map((item) => approvalDeadlineMs(item))
    .filter((d): d is number => d != null)
  const minDeadline = deadlines.length > 0 ? Math.min(...deadlines) : null

  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (minDeadline == null) return
    setNow(Date.now())
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [minDeadline])

  if (minDeadline == null) return `${items.length} 个待处理交互`
  const remaining = Math.max(0, Math.ceil((minDeadline - now) / 1000))
  return `${items.length} 项待决策 ${formatRemaining(remaining)}`
}

export function GlobalInteractionOverlay() {
  const pendingInteractions = useInteractionStore((s) => s.pendingInteractions)
  const isMinimized = useInteractionStore((s) => s.isMinimized)
  const toggleMinimized = useInteractionStore((s) => s.toggleMinimized)
  const dismissInteraction = useInteractionStore((s) => s.dismissInteraction)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)

  const { respondChoice, respondConversation, navigateToTab } =
    useInteractionHandler(activeSessionId ?? undefined)

  const [submittingId, setSubmittingId] = useState<string | null>(null)
  const [currentIndex, setCurrentIndex] = useState(0)

  // 过滤出 pending 状态的交互
  const pendingItems = pendingInteractions.filter((i) => i.status === 'pending')

  const currentInteraction = pendingItems[currentIndex] || null

  const composerClearance = useComposerClearance()
  const badgeText = useMinimizedBadgeText(pendingItems)

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
      } catch (error) {
        logger.module('InteractionOverlay').error('交互选项响应发送失败', { error })
        toast.error('交互响应发送失败，请重试')
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
      } catch (error) {
        logger.module('InteractionOverlay').error('交互文字响应发送失败', { error })
        toast.error('交互响应发送失败，请重试')
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
          currentInteraction.agentLevel,
        )
      } catch (error) {
        logger.module('InteractionOverlay').error('交互跳转响应发送失败', { error })
        toast.error('交互响应发送失败，请重试')
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

  // 最小化状态：显示收起徽标（BUG-43：小胶囊不遮 composer，点击展开）
  if (isMinimized) {
    if (pendingItems.length === 0) return null

    return createPortal(
      <button
        type="button"
        className="fixed right-4 bottom-4 z-[10000] flex cursor-pointer items-center gap-2 rounded-full bg-primary px-4 py-2 text-primary-foreground shadow-lg transition-colors hover:bg-primary/90"
        style={composerClearance > 0 ? { bottom: composerClearance } : undefined}
        onClick={toggleMinimized}
      >
        <span className="text-sm font-medium">{badgeText}</span>
      </button>,
      document.body,
    )
  }

  // 正常状态：显示交互卡片（右下悬浮，锚定 composer 上缘）
  if (!currentInteraction) return null

  return createPortal(
    <div
      data-testid="interaction-overlay-panel"
      className="fixed right-4 bottom-4 z-[10000] flex w-[min(26rem,calc(100vw-2rem))] flex-col items-stretch"
      style={composerClearance > 0 ? { bottom: composerClearance } : undefined}
    >
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
            <ChevronLeft className="h-icon-md w-icon-md" />
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
            <ChevronRight className="h-icon-md w-icon-md" />
          </button>
        </div>

        {/* 操作按钮 */}
        <div className="flex items-center gap-2">
          <button
            onClick={toggleMinimized}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-background/80 backdrop-blur-sm border border-border/50 shadow-sm hover:bg-background transition-colors"
            title="最小化"
          >
            <Minimize2 className="h-icon-md w-icon-md" />
          </button>
          <button
            onClick={handleDismiss}
            className="flex h-8 w-8 items-center justify-center rounded-full bg-background/80 backdrop-blur-sm border border-border/50 shadow-sm hover:bg-background transition-colors"
            title="关闭"
          >
            <X className="h-icon-md w-icon-md" />
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
    </div>,
    document.body,
  )
}
