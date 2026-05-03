/**
 * InteractionPanel 容器组件
 *
 * 薄容器层：读取 interactionStore + 调用 useInteractionHandler，
 * 将数据和 actions 以 props 传递给 InteractionCard。
 * 自身不包含业务逻辑。
 */

import { useState, useCallback, useEffect, useRef } from 'react'
import { useInteractionHandler } from '@/hooks/useInteractionHandler'
import { InteractionCard } from './InteractionCard'

interface InteractionPanelProps {
  sessionId?: string
}

export function InteractionPanel({ sessionId }: InteractionPanelProps) {
  const { pendingInteractions, respondChoice, respondConversation, navigateToTab } =
    useInteractionHandler(sessionId)

  const [submittingId, setSubmittingId] = useState<string | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  const handleRespondChoice = useCallback(
    async (requestId: string, optionId: string) => {
      if (submittingId && submittingId !== requestId) return
      setSubmittingId(requestId)
      try {
        await respondChoice(requestId, optionId)
      } finally {
        setSubmittingId(null)
      }
    },
    [respondChoice, submittingId],
  )

  const handleRespondText = useCallback(
    async (requestId: string, text: string) => {
      if (submittingId && submittingId !== requestId) return
      setSubmittingId(requestId)
      try {
        await respondConversation(requestId, text)
      } finally {
        setSubmittingId(null)
      }
    },
    [respondConversation, submittingId],
  )

  const handleNavigateToTab = useCallback(
    async (requestId: string, threadId: string) => {
      if (submittingId && submittingId !== requestId) return
      setSubmittingId(requestId)
      try {
        await navigateToTab(requestId, threadId)
      } finally {
        setSubmittingId(null)
      }
    },
    [navigateToTab, submittingId],
  )

  useEffect(() => {
    if (pendingInteractions.length > 0 && panelRef.current) {
      panelRef.current.scrollIntoView({ behavior: 'smooth', block: 'end' })
    }
  }, [pendingInteractions.length])

  if (pendingInteractions.length === 0) {
    return null
  }

  return (
    <div ref={panelRef} className="shrink-0 animate-in fade-in slide-in-from-bottom-2 duration-300">
      {pendingInteractions.map((interaction) => (
        <InteractionCard
          key={interaction.requestId}
          interaction={interaction}
          onRespondChoice={(optionId) =>
            handleRespondChoice(interaction.requestId, optionId)
          }
          onRespondText={(text) =>
            handleRespondText(interaction.requestId, text)
          }
          onNavigateToTab={() =>
            handleNavigateToTab(interaction.requestId, interaction.threadId)
          }
          isSubmitting={submittingId === interaction.requestId}
        />
      ))}
    </div>
  )
}
