/**
 * 弹性分栏组件
 *
 * 支持拖拽调整宽度、折叠展开
 */
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useState, useRef, useCallback } from 'react'
import { cn } from '@/lib/utils'

export interface SplitPaneProps {
  /** 左侧面板内容 */
  leftContent: React.ReactNode
  /** 右侧面板内容 */
  rightContent: React.ReactNode
  /** 默认左侧宽度百分比 (20-70) */
  defaultLeftWidth?: number
  /** 最小左侧宽度百分比 */
  minLeftWidth?: number
  /** 最大左侧宽度百分比 */
  maxLeftWidth?: number
  /** 初始是否折叠右侧 */
  initiallyCollapsed?: boolean
  /** 宽度变化回调 */
  onWidthChange?: (leftWidth: number) => void
  /** 折叠状态变化回调 */
  onCollapseChange?: (isCollapsed: boolean) => void
}

/**
 * 弹性分栏组件
 *
 * 支持鼠标拖拽调整左右面板宽度比例，以及折叠/展开右侧面板
 */
export const SplitPane: React.FC<SplitPaneProps> = ({
  leftContent,
  rightContent,
  defaultLeftWidth = 40,
  minLeftWidth = 20,
  maxLeftWidth = 70,
  initiallyCollapsed = false,
  onWidthChange,
  onCollapseChange,
}) => {
  const [leftWidth, setLeftWidth] = useState(defaultLeftWidth)
  const [isRightCollapsed, setIsRightCollapsed] = useState(initiallyCollapsed)
  const [isDragging, setIsDragging] = useState(false)
  const [lastExpandedWidth, setLastExpandedWidth] = useState(defaultLeftWidth)
  const containerRef = useRef<HTMLDivElement>(null)

  /**
   * 处理拖拽条鼠标按下事件
   *
   * 注册全局 mousemove/mouseup 事件实现拖拽调整宽度
   */
  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      setIsDragging(true)

      const startX = e.clientX
      const startWidth = leftWidth

      const handleMouseMove = (moveEvent: MouseEvent) => {
        if (!containerRef.current) return

        const containerWidth = containerRef.current.offsetWidth
        const deltaX = moveEvent.clientX - startX
        const deltaPercent = (deltaX / containerWidth) * 100
        const newWidth = Math.max(minLeftWidth, Math.min(maxLeftWidth, startWidth + deltaPercent))

        setLeftWidth(newWidth)
        onWidthChange?.(newWidth)
      }

      const handleMouseUp = () => {
        setIsDragging(false)
        document.removeEventListener('mousemove', handleMouseMove)
        document.removeEventListener('mouseup', handleMouseUp)
      }

      document.addEventListener('mousemove', handleMouseMove)
      document.addEventListener('mouseup', handleMouseUp)
    },
    [leftWidth, minLeftWidth, maxLeftWidth, onWidthChange],
  )

  /**
   * 切换右侧面板折叠/展开状态
   *
   * 展开时恢复记忆的宽度，折叠时记住当前宽度
   */
  const toggleCollapse = useCallback(() => {
    if (isRightCollapsed) {
      // 展开时恢复记忆的宽度
      setLeftWidth(lastExpandedWidth)
      setIsRightCollapsed(false)
      onCollapseChange?.(false)
    } else {
      // 折叠时记住当前宽度
      setLastExpandedWidth(leftWidth)
      setIsRightCollapsed(true)
      onCollapseChange?.(true)
    }
  }, [isRightCollapsed, leftWidth, lastExpandedWidth, onCollapseChange])

  return (
    <div ref={containerRef} className="flex h-full w-full overflow-hidden">
      {/* 左侧面板 */}
      <div
        className={cn(
          'overflow-hidden transition-all duration-300 ease-in-out',
          isRightCollapsed && 'flex-1',
        )}
        style={{
          width: isRightCollapsed ? '100%' : `${leftWidth}%`,
        }}
      >
        {leftContent}
      </div>

      {/* 拖拽条 */}
      <div
        className={cn(
          'group relative flex-shrink-0',
          'border-border/50 w-1 border-r',
          'cursor-col-resize transition-colors',
          isDragging && 'bg-status-running',
        )}
        onMouseDown={handleMouseDown}
      >
        {/* 折叠/展开按钮 */}
        <button
          onClick={toggleCollapse}
          className={cn(
            'absolute top-1/2 -translate-x-1/2 -translate-y-1/2',
            'glass-panel h-8 w-8 rounded-lg',
            'flex items-center justify-center',
            'opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity',
            'hover:bg-surface/80',
          )}
          title={isRightCollapsed ? '展开执行图' : '折叠执行图'}
        >
          {isRightCollapsed ? (
            <ChevronLeft className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
        </button>
      </div>

      {/* 右侧面板 */}
      <div
        className={cn(
          'relative flex-shrink-0 transition-all duration-300 ease-in-out',
          isRightCollapsed ? 'w-10' : 'flex-1',
        )}
      >
        {rightContent}
      </div>
    </div>
  )
}
