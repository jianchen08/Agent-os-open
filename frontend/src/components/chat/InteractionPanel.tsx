/** InteractionPanel 容器组件 薄容器层：读取 interactionStore + 调用 useInteractionHandler， */

import { useState, useCallback, useEffect, useRef } from 'react'
import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { useInteractionStore } from '@/stores/interactionStore'
import { InteractionCard } from './InteractionCard'

interface InteractionPanelProps {
  sessionId?: string
}

export function InteractionPanel({ sessionId }: InteractionPanelProps) {
  const { pendingInteractions, respondChoice, respondConversation, navigateToTab } =
    useInteractionHandler(sessionId)
  const dismissInteraction = useInteractionStore((s) => s.dismissInteraction)

  // // 安全措施：过滤掉 notification 模式的交互，确保通知模式不在聊天区域的 InteractionCard 中显示
  const nonNotificationInteractions = pendingInteractions.filter(
    (i) => i.mode !== 'notification',
  )

  const [submittingId, setSubmittingId] = useState<string | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  const handleRespondChoice = useCallback(
    async (requestId: string, optionId: string, optionLabel?: string) => {
      if (submittingId && submittingId !== requestId) return
      setSubmittingId(requestId)
      dismissInteraction(requestId)
      try {
        await respondChoice(requestId, optionLabel || optionId)
      } finally {
        setSubmittingId(null)
      }
    },
    [respondChoice, submittingId, dismissInteraction],
  )

  const handleRespondText = useCallback(
    async (requestId: string, text: string) => {
      if (submittingId && submittingId !== requestId) return
      setSubmittingId(requestId)
      dismissInteraction(requestId)
      try {
        await respondConversation(requestId, text)
      } finally {
        setSubmittingId(null)
      }
    },
    [respondConversation, submittingId, dismissInteraction],
  )

  const handleNavigateToTab = useCallback(
    async (requestId: string, pipelineId: string, title?: string, agentLevel?: string, interactionSessionId?: string) => {
      if (submittingId && submittingId !== requestId) return
      setSubmittingId(requestId)
      dismissInteraction(requestId)
      try {
        await navigateToTab(requestId, pipelineId, title, agentLevel, interactionSessionId)
      } finally {
        setSubmittingId(null)
      }
    },
    [navigateToTab, submittingId, dismissInteraction],
  )

  useEffect(() => {
    if (nonNotificationInteractions.length > 0 && panelRef.current) {
      panelRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' })
    }
  }, [nonNotificationInteractions.length])

  if (nonNotificationInteractions.length === 0) {
    return null
  }

  return (
    <div ref={panelRef} className="shrink-0 animate-in fade-in slide-in-from-bottom-2 duration-300">
      {nonNotificationInteractions.map((interaction) => (
        <InteractionCard
          key={interaction.requestId}
          interaction={interaction}
          onRespondChoice={(optionId, optionLabel) =>
            handleRespondChoice(interaction.requestId, optionId, optionLabel)
          }
          onRespondText={(text) =>
            handleRespondText(interaction.requestId, text)
          }
          onNavigateToTab={() =>
            handleNavigateToTab(interaction.requestId, interaction.pipelineId || interaction.threadId, interaction.title, interaction.agentLevel, interaction.sessionId)
          }
          onDismiss={() => dismissInteraction(interaction.requestId)}
          isSubmitting={submittingId === interaction.requestId}
        />
      ))}
    </div>
  )
}
