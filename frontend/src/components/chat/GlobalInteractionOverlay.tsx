/**
 * GlobalInteractionOverlay - 全局交互浮层组件
 *
 * 当用户从通知中心点击人类交互通知时，
 * 通过 interactionStore.globalOpenRequestId 触发，
 * 以浮动面板形式展示对应的 InteractionCard，
 * 不依赖当前所在的具体对话窗口。
 */

import { useCallback, useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { useInteractionStore } from '@/stores/interactionStore'
import { useSessionStore } from '@/stores/sessionStore'
import { InteractionCard } from './InteractionCard'

export function GlobalInteractionOverlay() {
  const globalOpenRequestId = useInteractionStore((s) => s.globalOpenRequestId)
  const setGlobalOpenRequestId = useInteractionStore((s) => s.setGlobalOpenRequestId)
  const pendingInteractions = useInteractionStore((s) => s.pendingInteractions)
  const dismissInteraction = useInteractionStore((s) => s.dismissInteraction)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)

  const { respondChoice, respondConversation, navigateToTab } =
    useInteractionHandler(activeSessionId)

  const [submittingId, setSubmittingId] = useState<string | null>(null)

  const interaction = globalOpenRequestId
    ? pendingInteractions.find((i) => i.requestId === globalOpenRequestId)
    : null

  useEffect(() => {
    if (globalOpenRequestId && !interaction) {
      setGlobalOpenRequestId(null)
    }
  }, [globalOpenRequestId, interaction, setGlobalOpenRequestId])

  useEffect(() => {
    if (!interaction) return
    if (interaction.status === 'responded' || interaction.status === 'navigated') {
      const timer = setTimeout(() => {
        setGlobalOpenRequestId(null)
        dismissInteraction(interaction.requestId)
      }, 2000)
      return () => clearTimeout(timer)
    }
  }, [interaction, setGlobalOpenRequestId, dismissInteraction])

  const handleClose = useCallback(() => {
    setGlobalOpenRequestId(null)
  }, [setGlobalOpenRequestId])

  const handleRespondChoice = useCallback(
    async (optionId: string) => {
      if (!interaction) return
      if (submittingId && submittingId !== interaction.requestId) return
      setSubmittingId(interaction.requestId)
      dismissInteraction(interaction.requestId)
      try {
        await respondChoice(interaction.requestId, optionId)
      } finally {
        setSubmittingId(null)
      }
      setGlobalOpenRequestId(null)
    },
    [interaction, respondChoice, submittingId, dismissInteraction, setGlobalOpenRequestId],
  )

  const handleRespondText = useCallback(
    async (text: string) => {
      if (!interaction) return
      if (submittingId && submittingId !== interaction.requestId) return
      setSubmittingId(interaction.requestId)
      dismissInteraction(interaction.requestId)
      try {
        await respondConversation(interaction.requestId, text)
      } finally {
        setSubmittingId(null)
      }
      setGlobalOpenRequestId(null)
    },
    [interaction, respondConversation, submittingId, dismissInteraction, setGlobalOpenRequestId],
  )

  const handleNavigateToTab = useCallback(
    async () => {
      if (!interaction) return
      if (submittingId && submittingId !== interaction.requestId) return
      setSubmittingId(interaction.requestId)
      try {
        await navigateToTab(
          interaction.requestId,
          interaction.threadId,
          interaction.title,
          (interaction as any).agentLevel,
          interaction.sessionId,
        )
      } finally {
        setSubmittingId(null)
      }
      setGlobalOpenRequestId(null)
    },
    [interaction, navigateToTab, submittingId, setGlobalOpenRequestId],
  )

  const handleDismiss = useCallback(() => {
    if (!interaction) return
    dismissInteraction(interaction.requestId)
    setGlobalOpenRequestId(null)
  }, [interaction, dismissInteraction, setGlobalOpenRequestId])

  useEffect(() => {
    if (!globalOpenRequestId) return

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setGlobalOpenRequestId(null)
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [globalOpenRequestId, setGlobalOpenRequestId])

  if (!interaction) return null

  return createPortal(
    <div
      className="fixed inset-0 z-[10000] flex items-center justify-center"
      onClick={handleClose}
    >
      <div className="absolute inset-0 bg-black/30" />
      <div
        className="relative z-10 mx-4 w-full max-w-lg animate-in fade-in zoom-in-95 duration-200"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          onClick={handleClose}
          className="absolute -top-2 -right-2 z-20 flex h-7 w-7 items-center justify-center rounded-full border bg-background shadow-sm hover:bg-accent transition-colors"
          title="关闭"
        >
          <X className="h-3.5 w-3.5" />
        </button>
        <InteractionCard
          interaction={interaction}
          onRespondChoice={handleRespondChoice}
          onRespondText={handleRespondText}
          onNavigateToTab={handleNavigateToTab}
          onDismiss={handleDismiss}
          isSubmitting={submittingId === interaction.requestId}
        />
      </div>
    </div>,
    document.body,
  )
}
