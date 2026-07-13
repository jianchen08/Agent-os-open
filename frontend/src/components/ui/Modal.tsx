/**
 * Modal 组件
 *
 * 使用 Portal 渲染到 body，避免被父容器的 overflow 裁剪
 */

import { X } from 'lucide-react'
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
  /** 点击背景是否关闭 */
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
  closeOnBackdropClick = true,
  maxWidth = '2xl',
}: ModalProps) {
  const modalRef = useRef<HTMLDivElement>(null)

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

  // 点击背景关闭
  const handleBackdropClick = (e: React.MouseEvent) => {
    if (closeOnBackdropClick && e.target === e.currentTarget) {
      onClose()
    }
  }

  if (!open) return null

  const modal = (
    <div
      ref={modalRef}
      className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
      style={{ zIndex: 9999, position: 'fixed', inset: 0 }}
      onClick={handleBackdropClick}
    >
      {/* 背景遮罩 */}
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
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
                <X className="h-5 w-5" />
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
            <X className="h-5 w-5" />
          </button>
        )}
      </div>
    </div>
  )

  // 使用 Portal 渲染到 body
  return createPortal(modal, document.body)
}
