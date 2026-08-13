/**
 * Modal 组件
 *
 * 使用 Portal 渲染到 body，避免被父容器的 overflow 裁剪
 */

import { XIcon } from '@/assets/icons'
import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'

export interface ModalProps {
  /** 是否显示模态框 */
  open: boolean
  /** 关闭回调 */
  onClose: () => void
  /** 标题 */
  title?: string
  /** 子内容 */
  children: React.ReactNode
  /** 自定义类名 */
  className?: string
  /** 是否显示关闭按钮 */
  showClose?: boolean
  /** 点击背景是否关闭（默认 false：含表单/输入的模态框误触会丢失内容，
   * 仅纯展示/确认类弹窗按需显式传 true） */
  closeOnBackdropClick?: boolean
  /** 最大宽度 */
  maxWidth?: 'sm' | 'md' | 'lg' | 'xl' | '2xl' | 'full'
}

const maxWidthClasses = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-xl',
  '2xl': 'max-w-2xl',
  full: 'max-w-full',
}

/**
 * Modal 组件
 *
 * 使用 Portal 渲染到 document.body，确保模态框始终在最上层
 */
export function Modal({
  open,
  onClose,
  title,
  children,
  className,
  showClose = true,
  closeOnBackdropClick = false,
  maxWidth = '2xl',
}: ModalProps) {
  const modalRef = useRef<HTMLDivElement>(null)
  // 记录 mousedown 起始时是否点在遮罩（外层容器）上。
  // 用于区分"真正点击遮罩关闭"与"在输入框拖选文字时鼠标移出触发的合成 click"：
  // 后者 mousedown 在输入框内、mouseup 在遮罩上，浏览器会合成一个 target=遮罩的
  // click，导致误关闭。改为 mousedown/mouseUp 双判断即可规避。
  const mouseDownOnBackdrop = useRef(false)

  // ESC 键关闭
  useEffect(() => {
    if (!open) return

    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose()
      }
    }

    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [open, onClose])

  // 禁止背景滚动
  useEffect(() => {
    if (!open) return

    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = ''
    }
  }, [open])

  // 遮罩点击关闭：mousedown + mouseUp 都在遮罩上才关闭。
  // 仅用 click 会在"输入框拖选文字、鼠标移出"时误触发（mousedown 与 mouseup
  // 跨元素，浏览器合成的 click target 落在公共祖先遮罩上）。
  const handleBackdropMouseDown = (e: React.MouseEvent) => {
    mouseDownOnBackdrop.current = closeOnBackdropClick && e.target === e.currentTarget
  }

  const handleBackdropMouseUp = (e: React.MouseEvent) => {
    if (mouseDownOnBackdrop.current && e.target === e.currentTarget) {
      onClose()
    }
    mouseDownOnBackdrop.current = false
  }

  if (!open) return null

  const modal = (
    <div
      ref={modalRef}
      className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
      style={{ zIndex: 9999, position: 'fixed', inset: 0 }}
      onMouseDown={handleBackdropMouseDown}
      onMouseUp={handleBackdropMouseUp}
    >
      {/* 背景遮罩 */}
      <div
        className="absolute inset-0 bg-[var(--overlay-bg)] backdrop-blur-sm"
        style={{
          position: 'absolute',
          inset: 0,
          backgroundColor: 'rgba(0,0,0,0.5)',
        }}
      />

      {/* 模态框内容 */}
      <div
        className={cn(
          'bg-card text-card-foreground relative rounded-lg shadow-xl',
          'max-h-[90vh] overflow-auto',
          'animate-in fade-in-0 zoom-in-95 duration-200',
          maxWidthClasses[maxWidth],
          className,
        )}
        style={{
          position: 'relative',
          width: '100%',
          backgroundColor: 'hsl(var(--card))',
          color: 'hsl(var(--foreground))',
          opacity: 1,
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 标题栏 */}
        {title && (
          <div className="flex items-center justify-between border-b p-6">
            <h2 className="text-lg font-semibold">{title}</h2>
            {showClose && (
              <button
                onClick={onClose}
                className="text-muted-foreground hover:text-foreground transition-colors"
                aria-label="关闭"
              >
                <XIcon className="h-5 w-5" />
              </button>
            )}
          </div>
        )}

        {/* 内容区 */}
        <div className={cn('p-6', !title && showClose && 'pt-6')}>{children}</div>

        {/* 无标题时的关闭按钮 */}
        {!title && showClose && (
          <button
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground absolute top-4 right-4 transition-colors"
            aria-label="关闭"
          >
            <XIcon className="h-5 w-5" />
          </button>
        )}
      </div>
    </div>
  )

  // 使用 Portal 渲染到 body
  return createPortal(modal, document.body)
}
