/**
 * SchemaFullscreenHost — ui_schema 声明驱动的全屏浮层宿主（架构 §5.3 落点）
 *
 * 让「声明 → 实现」链路长出全屏交互界面，替换硬编码审批浮层：
 * 1. 收集 space="fullscreen" 且 trigger="on_event:xxx" 的 widget 声明
 *    （contributionRegistry.getAllWidgets()，即插件 ui_schema 推送的声明）
 * 2. 从 trigger 求值出事件名（on_event:approval.created → "approval.created"），
 *    动态订阅 globalWS 事件——这是统一的事件驱动 widget 机制（A5）
 * 3. 事件到达 → 打开 FullscreenOverlay → DeclaredWidgetLayer 渲染声明 widget，
 *    事件 payload 合并进声明 props（widget 拿到真实数据：artifacts/annotations…）
 * 4. 通用底部操作栏按 payload.mode 提供交互，提交走既有真实通道：
 *    - choice / conversation / review → globalWS.sendInteractionResponse
 *      （内核 ws 路由白名单只收 interaction_response，不收 type:'approval'——
 *      review 曾走 sendApproval，消息会被内核 RouteOutcome::Ignored 静默丢弃）
 *
 * 扩展点语义：任何插件声明 fullscreen + on_event 的 widget，无需手写前端即可
 * 长出全屏面板。approval/plugin.json 的 approval_panel（review_document，
 * trigger: "on_event:approval.created"）即首个消费者。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight } from '@/assets/icons'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useSessionStore } from '@/stores/sessionStore'
import type { WidgetDeclaration } from '@/services/schema/ContributionRegistry'
import { DeclaredWidgetLayer } from './DeclaredWidgetLayer'
import { FullscreenOverlay } from '@/components/layout/FullscreenOverlay'

/** 事件驱动浮层的标准化条目 */
interface SchemaEventItem {
  /** 命中的声明 ID */
  declarationId: string
  /** 触发事件名 */
  eventName: string
  /** 原始事件 payload（合并进声明 props 传给 widget） */
  payload: Record<string, unknown>
  /** 请求 ID（去重 / 提交回执） */
  requestId: string
  title: string
  mode: 'review' | 'choice' | 'conversation'
  options: string[]
}

/** 从声明 trigger（"on_event:xxx"）求值事件名；非 on_event 触发返回 null */
export function parseEventTrigger(trigger: string | undefined): string | null {
  if (!trigger) return null
  const m = /^on_event:(.+)$/.exec(trigger)
  return m ? m[1] : null
}

/** 收集指定 space 下 trigger=on_event:* 的声明（可注入，测试隔离） */
export function collectFullscreenEventDeclarations(
  declarations: WidgetDeclaration[],
  space = 'fullscreen',
): Array<{ declaration: WidgetDeclaration; eventName: string }> {
  const out: Array<{ declaration: WidgetDeclaration; eventName: string }> = []
  for (const d of declarations) {
    if (d.space && d.space !== space) continue
    const eventName = parseEventTrigger(d.trigger)
    if (eventName) out.push({ declaration: d, eventName })
  }
  return out
}

/** 从原始事件数据提取 payload（兼容 data 嵌套与扁平两种封装） */
function extractRawPayload(rawData: Record<string, unknown>): Record<string, unknown> {
  return (rawData.data as Record<string, unknown>) || rawData
}

/** 事件 payload → 标准化条目（与后端 approval.created payload 对齐） */
export function toSchemaEventItem(
  declarationId: string,
  eventName: string,
  rawData: Record<string, unknown>,
): SchemaEventItem | null {
  const data = extractRawPayload(rawData)
  const requestId = (data.request_id as string) || (data.approval_id as string)
  if (!requestId) return null
  const mode = (data.mode as SchemaEventItem['mode']) || 'review'
  return {
    declarationId,
    eventName,
    payload: data,
    requestId,
    title:
      (data.title as string) ||
      (data.summary as string) ||
      (data.message as string) ||
      (mode === 'choice' ? '请选择' : mode === 'conversation' ? '请回复' : '审阅请求'),
    mode,
    options: Array.isArray(data.options) ? (data.options as string[]) : [],
  }
}

export interface SchemaFullscreenHostProps {
  /** 注入声明（缺省读 contributionRegistry.getAllWidgets()，即生产消费链路） */
  declarations?: WidgetDeclaration[]
  /** 目标空间（缺省 fullscreen） */
  space?: string
}

/**
 * 全屏声明浮层宿主
 *
 * 挂载点：ProtectedRoute 内（与 GlobalInteractionOverlay 同级），全局可见。
 */
export function SchemaFullscreenHost({
  declarations,
  space = 'fullscreen',
}: SchemaFullscreenHostProps) {
  const [eventDecls, setEventDecls] = useState<Array<{ declaration: WidgetDeclaration; eventName: string }>>([])
  const [queue, setQueue] = useState<SchemaEventItem[]>([])
  const [currentIndex, setCurrentIndex] = useState(0)
  const [submittingId, setSubmittingId] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  // 声明收集（trigger 求值 + space 过滤）。getAllWidgets() 每次返回新数组引用，
  // 只在依赖变化时重算，避免订阅 effect 反复重挂。
  useEffect(() => {
    const decls = declarations ?? contributionRegistry.getAllWidgets()
    setEventDecls(collectFullscreenEventDeclarations(decls, space))
  }, [declarations, space])

  // 为每个触发事件名注册订阅；事件到达 → 入队（declarationId + requestId 去重）
  useEffect(() => {
    const handlers = new Map<string, (raw: Record<string, unknown>) => void>()
    for (const { declaration, eventName } of eventDecls) {
      const handler = (raw: Record<string, unknown>) => {
        const item = toSchemaEventItem(declaration.id, eventName, raw)
        if (!item) return
        setQueue((prev) => {
          if (prev.some((p) => p.declarationId === declaration.id && p.requestId === item.requestId)) {
            return prev
          }
          return [...prev, item]
        })
      }
      handlers.set(eventName, handler)
      globalWS.subscribe(eventName, handler as never)
    }
    return () => {
      for (const [eventName, handler] of handlers) {
        globalWS.unsubscribe(eventName, handler as never)
      }
    }
  }, [eventDecls])

  const current = queue[currentIndex] || null

  // 自动校正索引（队尾移除后回退）
  useEffect(() => {
    if (currentIndex >= queue.length) {
      setCurrentIndex(Math.max(0, queue.length - 1))
    }
  }, [queue.length, currentIndex])

  const removeItem = useCallback((requestId: string) => {
    setQueue((prev) => prev.filter((p) => p.requestId !== requestId))
  }, [])

  /** 当前命中的声明（props 合并事件 payload 后传给 widget） */
  const activeDecl = useMemo(() => {
    if (!current) return null
    const found = eventDecls.find((e) => e.declaration.id === current.declarationId)
    if (!found) return null
    return {
      ...found.declaration,
      props: { ...found.declaration.props, ...current.payload },
    }
  }, [current, eventDecls])

  const handleSubmit = useCallback(
    async (result: string, extra?: Record<string, unknown>) => {
      if (!current || (submittingId && submittingId !== current.requestId)) return
      setSubmittingId(current.requestId)
      try {
        const sid = useSessionStore.getState().activeSessionId
        const threadId =
          sid ||
          (typeof current.payload.thread_id === 'string' ? current.payload.thread_id : '') ||
          (typeof current.payload.run_id === 'string' ? current.payload.run_id : '') ||
          'approval'
        // review 与 choice 统一走 interaction_response：内核 ws 白名单
        // （kernel session router）只接受 interaction_response，type:'approval' 会被忽略。
        // review 的 decision（approved/rejected）放 selected_option，附 reason。
        globalWS.sendInteractionResponse(threadId, current.requestId, {
          response_type: 'answered',
          selected_option: result,
          feedback: extra?.feedback,
          reason: (extra?.reason as string) ?? '',
        })
        removeItem(current.requestId)
      } finally {
        setSubmittingId(null)
      }
    },
    [current, submittingId, removeItem],
  )

  if (!current) return null

  const closeCurrent = () => removeItem(current.requestId)
  const isSubmitting = submittingId === current.requestId

  return (
    <FullscreenOverlay isActive title={current.title} onExit={closeCurrent}>
      <div className="flex h-full flex-col">
        {/* 导航行：队列切换 + 事件来源标识 */}
        <div className="flex items-center justify-between border-b px-4 py-2">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setCurrentIndex((p) => Math.max(0, p - 1))}
              disabled={currentIndex === 0}
              className="border-border/50 hover:bg-muted flex h-8 w-8 items-center justify-center rounded-full border disabled:opacity-30"
              title="上一个"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <span className="text-muted-foreground min-w-[60px] text-center text-sm">
              {currentIndex + 1} / {queue.length}
            </span>
            <button
              type="button"
              onClick={() => setCurrentIndex((p) => Math.min(queue.length - 1, p + 1))}
              disabled={currentIndex === queue.length - 1}
              className="border-border/50 hover:bg-muted flex h-8 w-8 items-center justify-center rounded-full border disabled:opacity-30"
              title="下一个"
            >
              <ChevronRight className="h-4 w-4" />
            </button>
          </div>
          <span className="text-muted-foreground text-xs">
            {current.mode} · {current.eventName}
          </span>
        </div>

        {/* 声明 widget 渲染（payload 已并入 props） */}
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {activeDecl ? (
            <DeclaredWidgetLayer space={space} declarations={[activeDecl]} />
          ) : (
            <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
              暂无 widget 声明（trigger: on_event:{current.eventName}）
            </div>
          )}
        </div>

        {/* 通用操作栏（按 mode 提供交互，提交走既有真实通道） */}
        <div className="flex flex-col gap-2 border-t px-4 py-3">
          {current.mode === 'choice' && (
            <div className="flex flex-wrap gap-2">
              {current.options.map((opt) => (
                <button
                  key={opt}
                  type="button"
                  onClick={() => handleSubmit(opt)}
                  disabled={isSubmitting}
                  className="bg-primary/10 text-primary hover:bg-primary/20 rounded-md px-4 py-2 text-sm disabled:opacity-50"
                >
                  {opt}
                </button>
              ))}
            </div>
          )}

          {current.mode === 'conversation' && (
            <div className="flex gap-2">
              <textarea
                className="border-border text-foreground bg-background h-24 flex-1 rounded-md border p-3 text-sm"
                placeholder="请输入回复..."
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                disabled={isSubmitting}
              />
              <button
                type="button"
                onClick={() => {
                  handleSubmit(draft, { feedback: draft })
                  setDraft('')
                }}
                disabled={isSubmitting || !draft.trim()}
                className="bg-primary text-primary-foreground hover:bg-primary/90 self-end rounded-md px-4 py-2 text-sm disabled:opacity-50"
              >
                提交
              </button>
            </div>
          )}

          {current.mode === 'review' && (
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => handleSubmit('rejected')}
                disabled={isSubmitting}
                className="bg-destructive/10 text-destructive hover:bg-destructive/20 rounded-md px-4 py-2 text-sm disabled:opacity-50"
              >
                拒绝
              </button>
              <button
                type="button"
                onClick={() => handleSubmit('approved')}
                disabled={isSubmitting}
                className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-md px-4 py-2 text-sm disabled:opacity-50"
              >
                {isSubmitting ? '提交中...' : '批准'}
              </button>
            </div>
          )}
        </div>
      </div>
    </FullscreenOverlay>
  )
}
