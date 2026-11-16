/**
 * 全屏简化 composer 浮窗（2026-11-05 阶段一件一）
 *
 * 工作区全屏（uiStore.workspaceFullscreen）时，真实聊天输入框被 CSS 隐藏
 * （FiveSpaceLayout 保挂载），「引用/评审」落进输入框的内容用户看不见——
 * 交互断链。本浮窗是真实输入框的镜像视图，与主输入框同源同内容、仅显示简化
 * （用户拍板 2026-11-05，调研 §6.1-3）：
 *   - 文本：真源 = chatInputStore.drafts[draftKey]（主输入框每次编辑实时
 *     saveDraft；浮窗编辑实时 saveDraft，主输入框订阅同步回来）；
 *   - 引用：同挂 ReferenceSelectionRow（同一 provider 状态第二实例）；
 *   - 发送：只触发 requestRemoteSend，由挂载中的真实 ChatInput 以自身完整
 *     状态（附件/模型/思考强度等局部参数）走同一 handleSend 执行——本组件
 *     不拼参（禁复制发送逻辑）。
 * 不做（阶段二/遗留）：拖拽/缩放/设置项/移动端布局/layoutModeStore overlay
 * 全屏变体。
 *
 * R7 Bug②（长卡片顶出浮窗）：右下角定位（bottom-4 right-4，用户裁定不挡制品
 * 中央内容）+ 容器 max-h-[60vh]（flex-col：操作行 shrink-0 恒可见，引用镜像行
 * 与反馈区在上部可滚动区兜底）+ 卡片行自身封高 max-h-32 滚动 + 输入容器
 * min-w-0（窄容器横向防撑破）。
 */

import { useEffect, useRef, useState } from 'react'
import { Loader2, Minimize2, Send } from '@/assets/icons'
import { CitationCardRow } from '@/components/chat/CitationCardRow'
import { ReferenceSelectionRow } from '@/components/chat/ReferenceSelectionRow'
import { useAutoGrowTextarea } from '@/hooks/useAutoGrowTextarea'
import { cn } from '@/lib/utils'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useChatInputStore, type SendOutcome } from '@/stores/chatInputStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'

/** 落定「已发送」轻反馈自动消退时长 */
const LANDED_FEEDBACK_MS = 2500

/** 正文 auto-grow 高度封顶（px）= max-h-40（10rem），两处保持一致 */
const FULLSCREEN_TEXTAREA_MAX_HEIGHT = 160

/** 发送终局 → 浮窗提示文案（与 useSendOutcomeConsumer 操作点文案一致） */
function outcomeFeedback(outcome: SendOutcome): { kind: 'ok' | 'error'; message: string } | null {
  if (outcome.kind === 'landed') return { kind: 'ok', message: '已发送' }
  if (outcome.kind === 'timeout') return { kind: 'error', message: '发送超时，已停止执行——请重新发送' }
  return {
    kind: 'error',
    message: outcome.reason ? `消息发送失败：${outcome.reason}` : '消息发送失败，请重新发送',
  }
}

/** 全屏简化 composer 浮窗（内部按 workspaceFullscreen 门控，非全屏渲染 null） */
export function FullscreenComposer() {
  const workspaceFullscreen = useUIStore((s) => s.workspaceFullscreen)
  const setWorkspaceFullscreen = useUIStore((s) => s.setWorkspaceFullscreen)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  const tabs = useAgentTabStore((s) => s.tabs)

  /** 草稿键与 ChatContainer 接线同构：activeTabId || sessionId */
  const draftKey = activeTabId || activeSessionId || undefined
  const draftText = useChatInputStore((s) => (draftKey ? (s.drafts[draftKey] ?? '') : ''))
  const saveDraft = useChatInputStore((s) => s.saveDraft)
  const requestRemoteSend = useChatInputStore((s) => s.requestRemoteSend)
  const consumeRemoteSend = useChatInputStore((s) => s.consumeRemoteSend)
  const clearRemoteSendRefusal = useChatInputStore((s) => s.clearRemoteSendRefusal)
  const refusal = useChatInputStore((s) => s.remoteSendRefusal)

  /** 本管道发送态（与 ChatContainer 派生同源：activeTab.pipelineRunId） */
  const pipelineId = tabs.find((t) => t.id === activeTabId)?.pipelineRunId || ''
  const sendInFlight = useChatInputStore((s) =>
    pipelineId ? (s.sendInFlightByPipeline[pipelineId]?.length ?? 0) > 0 : false,
  )

  /** 发送终局反馈（浮窗态提示）：只读订阅不消费——终局消费的持有者仍是真实
   *  ChatInput（useSendOutcomeConsumer），本组件仅显示 */
  const [feedback, setFeedback] = useState<{ kind: 'ok' | 'error'; message: string } | null>(null)
  const lastOutcomeIdRef = useRef(0)
  useEffect(() => {
    if (!draftKey) return
    // 基线 = 挂载/切键时现存最大 outcome id：只看其后新增终局，不重放历史
    const existing = useChatInputStore.getState().sendOutcomes
    lastOutcomeIdRef.current =
      existing.length > 0 ? existing[existing.length - 1].id : 0
    const watchOutcomes = () => {
      for (const outcome of useChatInputStore.getState().sendOutcomes) {
        if (outcome.id <= lastOutcomeIdRef.current) continue
        lastOutcomeIdRef.current = outcome.id
        if (outcome.draftKey !== draftKey) continue
        setFeedback(outcomeFeedback(outcome))
      }
    }
    watchOutcomes()
    return useChatInputStore.subscribe(watchOutcomes)
  }, [draftKey])

  /** 落定反馈自动消退（错误反馈持续到下次发送尝试，不自动清） */
  useEffect(() => {
    if (feedback?.kind !== 'ok') return
    const timer = setTimeout(() => setFeedback(null), LANDED_FEEDBACK_MS)
    return () => clearTimeout(timer)
  }, [feedback])

  /** 卸载（退出全屏/布局卸载）时丢弃未消费的远程发送请求，防陈旧请求在下次
   *  ChatInput 挂载时自发触发发送 */
  useEffect(() => {
    return () => {
      consumeRemoteSend()
    }
  }, [consumeRemoteSend])

  /** 正文 auto-grow：草稿变化（手动输入/外部写入/引用插入同路）即重算高度 */
  const textareaRef = useAutoGrowTextarea<HTMLTextAreaElement>(draftText, {
    maxHeight: FULLSCREEN_TEXTAREA_MAX_HEIGHT,
  })

  if (!workspaceFullscreen) return null

  const handleTextChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    if (draftKey) saveDraft(draftKey, e.target.value)
  }

  const handleSend = () => {
    clearRemoteSendRefusal()
    setFeedback(null)
    requestRemoteSend()
  }

  const handleExitFullscreen = () => {
    setWorkspaceFullscreen(false)
  }

  return (
    <div
      data-testid="fullscreen-composer"
      className={cn(
        // 右下角画中画式迷你窗（R7 Bug② 用户裁定：不挡制品中央内容，发送/退出
        // 触手可及；被否=底部居中挡内容、可拖拽=阶段二遗留）；
        // 限高 max-h-[60vh] + flex-col：操作行 shrink-0 恒可见，上部可滚动区兜底
        'fixed bottom-4 right-4 z-40 flex max-h-[60vh] w-[min(480px,calc(100vw-2rem))] flex-col',
        'rounded-lg border border-border/60 bg-popover shadow-lg backdrop-blur',
      )}
    >
      {/* 可滚动区（限高兜底）：引用镜像行 + 落定/错误反馈。引用镜像与主输入框
          同一 provider 状态的第二实例，全屏下引用/评议进输入框实时可见 */}
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="px-3 pt-2">
          <ReferenceSelectionRow threadId={activeSessionId ?? undefined} />
        </div>
        {/* 落定轻反馈（成功态）：与错误/拒绝面分离——「已发送」是状态确认，
            不用告警色也不用 alert 语义（2.5s 后自动消退，见上反馈定时器） */}
        {feedback?.kind === 'ok' && (
          <div
            data-testid="fullscreen-composer-feedback"
            className="text-muted-foreground px-3 pb-2 text-xs"
            role="status"
          >
            {feedback.message}
          </div>
        )}
        {(refusal || feedback?.kind === 'error') && (
          <div
            data-testid="fullscreen-composer-error"
            className="text-status-warning px-3 pb-2 text-xs"
            role="alert"
          >
            {refusal?.reason ?? feedback?.message}
          </div>
        )}
      </div>
      {/* 操作行（shrink-0 恒可见不滚走）：正文输入容器 + 发送/退出。输入容器
          min-w-0（flex 子项可收缩，窄容器横向防撑破）；卡片行自身封高 max-h-32
          滚动（R7 Bug②），正文 textarea auto-grow 封顶 max-h-40 */}
      <div className="flex shrink-0 items-end gap-2 p-3 pt-1">
        <div
          data-testid="fullscreen-composer-input-box"
          className={cn(
            'min-w-0 flex-1 rounded-md border border-border/60 bg-background',
            // 焦点高亮整体化：环移到容器 focus-within（卡片行+正文一起高亮，
            // R6 容器内形态；原 textarea 自身 focus 环随之迁出）
            'focus-within:ring-1 focus-within:ring-ring',
          )}
        >
          <CitationCardRow draftKey={draftKey} scrollMaxClass="max-h-32" />
          <textarea
            ref={textareaRef}
            data-testid="fullscreen-composer-textarea"
            className={cn(
              'text-foreground placeholder:text-muted-foreground max-h-40 min-h-[2.5rem] w-full resize-none',
              'border-0 bg-transparent px-2 py-1.5 text-sm',
              'focus:outline-none',
            )}
            placeholder="输入消息（与主输入框同步）"
            value={draftText}
            onChange={handleTextChange}
            disabled={sendInFlight}
            rows={2}
          />
        </div>
        <button
          type="button"
          data-testid="fullscreen-composer-send"
          className={cn(
            'bg-primary text-primary-foreground flex h-9 w-9 shrink-0 items-center justify-center rounded-md',
            'disabled:cursor-not-allowed disabled:opacity-60',
          )}
          onClick={handleSend}
          disabled={sendInFlight}
          title="发送（与主输入框同参）"
          aria-label="发送"
        >
          {sendInFlight ? (
            <Loader2 className="h-icon-md w-icon-md animate-spin" />
          ) : (
            <Send className="h-icon-md w-icon-md" />
          )}
        </button>
        <button
          type="button"
          data-testid="fullscreen-composer-exit"
          className={cn(
            'text-muted-foreground hover:text-foreground flex h-9 w-9 shrink-0 items-center justify-center rounded-md',
            'hover:bg-accent border border-border/60',
          )}
          onClick={handleExitFullscreen}
          title="退出全屏回会话"
          aria-label="退出全屏"
        >
          <Minimize2 className="h-icon-md w-icon-md" />
        </button>
      </div>
    </div>
  )
}
