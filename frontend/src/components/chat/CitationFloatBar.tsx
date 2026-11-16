/**
 * 引用/评审浮动条（选中文字浮层，2026-11-05 R4 件③共通组件）。
 *
 * 自 CodeEditor.FloatingQuoteButton 上移（工作区制品与消息流共用同一形态，
 * 用户裁定：消息流引用/评审入口=工作区制品同款选中浮层，非常驻按钮+输入框）：
 *  - 视觉契约：注入式 CSS 变量（--floating-quote-*）+ floatingQuoteIn keyframes
 *    原样迁入（一次性注入 document.head，模块级单例）；
 *  - 「引用」→ onQuote；「评审」→ onReview(评语)，评语可选（空 = 等价纯引用）。
 *    内容组装真源在消费方（utils/citationCompose / CodeEditor.buildQuotedFragment），
 *    本组件不关心内容契约；
 *  - 评语输入框 auto-grow（R4 件① hook，封顶 max-h-24=96px）；
 *  - Esc 关闭；可编辑元素（评语输入框）内的 Escape 不关——输入法候选激活/
 *    取消的 Escape 不是关闭语义（件②同口径，utils/editableTarget 共通判定）；
 *  - 浮条上 pointerDown preventDefault 防点击时文档选区塌陷（评语 textarea
 *    除外，放行原生聚焦）；
 *  - 输入框点击保护区（R7 Bug①）：浮条活跃期 mousedown 落输入框区域
 *    （utils/inputBoxTarget 三容器判定）→ preventDefault + 手动聚焦——选区
 *    高亮与浮条双保持，输入框同时可打字；浮条外其余区域关闭语义不变；
 *  - positioning：默认 'absolute'（CodeEditor 容器内坐标）；'fixed'（消息流，
 *    视口坐标 + portal 到 body——气泡 overflow-hidden 不裁剪 portal 内容）。
 */

import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { Quote, X } from '@/assets/icons'
import { useAutoGrowTextarea } from '@/hooks/useAutoGrowTextarea'
import { isEditableEventTarget } from '@/utils/editableTarget'
import { closestInputBoxFromEvent } from '@/utils/inputBoxTarget'

/** floatingQuoteIn keyframes（一次性注入，模块级单例防重复 <style>） */
let _floatBarStyleInjected = false
function injectCitationFloatBarStyles(): void {
  if (_floatBarStyleInjected || typeof document === 'undefined') return
  _floatBarStyleInjected = true
  const style = document.createElement('style')
  style.textContent = `@keyframes floatingQuoteIn{from{opacity:0;transform:translate(-50%,-100%) scale(0.95)}to{opacity:1;transform:translate(-50%,-100%) scale(1)}}`
  document.head.appendChild(style)
}

export interface CitationFloatBarProps {
  /** 定位坐标（absolute=容器内坐标 / fixed=视口坐标） */
  position: { x: number; y: number }
  /** 定位形态：'absolute'（默认，CodeEditor 容器内）| 'fixed'（消息流视口 portal） */
  positioning?: 'absolute' | 'fixed'
  /** 「引用」动作（内容组装与落点由消费方承担） */
  onQuote: () => void
  /** 「评审」动作：参数=评语原文（空串=等价纯引用） */
  onReview: (comment: string) => void
  /** 关闭（Esc / × 按钮 / 消费方的选区消失与滚动守卫） */
  onClose: () => void
}

export function CitationFloatBar({
  position,
  positioning = 'absolute',
  onQuote,
  onReview,
  onClose,
}: CitationFloatBarProps) {
  /** 评审意见（可选；空 = 「评审」等价纯引用块） */
  const [comment, setComment] = useState('')
  /** 评语 auto-grow（件① hook）：封顶 96px = max-h-24 */
  const commentRef = useAutoGrowTextarea<HTMLTextAreaElement>(comment, { maxHeight: 96 })

  useEffect(() => {
    injectCitationFloatBarStyles()
  }, [])

  /** Esc 关闭；评语输入框内的 Escape 不关（输入法候选取消不是关闭语义） */
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !isEditableEventTarget(e)) onClose()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [onClose])

  /** 浮条上按下 preventDefault 防选区塌陷；评语 textarea 放行原生聚焦 */
  const handlePointerDown = (e: React.PointerEvent) => {
    if (e.target instanceof HTMLTextAreaElement) return
    e.preventDefault()
  }

  /**
   * 输入框点击保护区（R7 Bug① 统一语义，浮条活跃期挂载=卸载即移除）：
   * document 级 mousedown capture——事件落输入框区域（主输入框容器/全屏浮窗
   * 容器，utils/inputBoxTarget 判定）时 preventDefault（挡浏览器默认的「清除
   * 文档选区+原生聚焦」），随后手动聚焦被点中的可编辑件（Chrome 下 focus 进
   * textarea 不清 document selection → 选区高亮与浮条双保持、输入框可打字）。
   * 权衡（登记台账 R7）：浮条活跃期间点击输入框不做光标点击定位（光标落末尾），
   * 浮条关闭后恢复正常；容器内交互件（按钮等）只挡默认聚焦不重定向焦点，
   * click 语义不变。
   */
  useEffect(() => {
    const onMouseDown = (e: MouseEvent) => {
      const box = closestInputBoxFromEvent(e)
      if (!box) return
      e.preventDefault()
      const target = e.target instanceof HTMLElement ? e.target : null
      const editable = target?.closest('textarea, input')
      if (editable) {
        ;(editable as HTMLElement).focus()
        return
      }
      const interactive = target?.closest('button, a, select, label')
      if (!interactive) box.querySelector('textarea')?.focus()
    }
    document.addEventListener('mousedown', onMouseDown, true)
    return () => document.removeEventListener('mousedown', onMouseDown, true)
  }, [])

  const bar = (
    <div
      className={`pointer-events-auto z-50 ${positioning === 'fixed' ? 'fixed' : 'absolute'}`}
      /* eslint-disable-next-line no-restricted-syntax -- 动态定位值（选区坐标逐次变化），
         Tailwind 静态类无法承载；transform/animation 为浮层定位契约（自 CodeEditor 原样迁入） */
      style={{
        left: position.x,
        top: position.y,
        transform: 'translate(-50%, -100%)',
        animation: 'floatingQuoteIn 150ms ease-out',
      }}
      onPointerDown={handlePointerDown}
      data-testid="citation-float-bar"
    >
      <div className="flex items-center gap-1 rounded-lg border border-[var(--floating-quote-border)] bg-[var(--floating-quote-bg)] px-1.5 py-1 shadow-[var(--floating-quote-shadow,0_4px_12px_rgba(0,0,0,0.3))]">
        <button
          onClick={onQuote}
          className="flex items-center gap-1.5 whitespace-nowrap rounded-md px-2.5 py-1 text-xs font-medium text-[var(--floating-quote-text)] transition-colors hover:bg-[var(--floating-quote-hover-bg)]"
          title="引用到对话 (Enter)"
        >
          <Quote className="h-icon-sm w-icon-sm" />
          <span>引用</span>
        </button>
        <textarea
          ref={commentRef}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          placeholder="评审意见(可选)"
          rows={1}
          className="text-[var(--floating-quote-text)] placeholder:text-[var(--floating-quote-text)]/50 min-h-6 w-28 max-h-24 resize-none rounded border border-[var(--floating-quote-border)] bg-transparent px-1.5 py-0.5 text-xs focus:outline-none"
          data-testid="floating-quote-comment"
        />
        <button
          onClick={() => onReview(comment)}
          className="whitespace-nowrap rounded-md px-2 py-1 text-xs font-medium text-[var(--floating-quote-text)] transition-colors hover:bg-[var(--floating-quote-hover-bg)]"
          title="引用+评审说明合成一个条目进输入框"
          data-testid="floating-quote-review"
        >
          评审
        </button>
        <button
          onClick={onClose}
          className="text-[var(--floating-quote-text)] flex h-icon-md w-icon-md items-center justify-center rounded opacity-60 transition-opacity hover:opacity-100"
          title="关闭 (Esc)"
          aria-label="关闭"
        >
          <X className="h-icon-sm w-icon-sm" />
        </button>
      </div>
    </div>
  )

  if (positioning === 'fixed') {
    return createPortal(bar, document.body)
  }
  return bar
}
