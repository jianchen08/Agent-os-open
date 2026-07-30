/**
 * 主题切换按钮（单图标）
 *
 * - 单击：浅/深切换
 * - 悬停：弹出小窗可选全部主题
 */

import { Moon, Sun } from '@/assets/icons'
import { useCallback, useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import { useThemeStore } from '@/stores/themeStore'
import { ThemePopover } from './ThemePanel'

interface ThemeButtonProps {
  /** 额外 class */
  className?: string
  /** 图标尺寸 */
  compact?: boolean
}

/**
 * 单图标主题切换：点击切换深浅，悬停打开选主题小窗
 */
export function ThemeButton({ className, compact = true }: ThemeButtonProps) {
  const { resolvedTheme, setTheme } = useThemeStore()
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)
  const leaveTimer = useRef<number | null>(null)

  const clearLeave = () => {
    if (leaveTimer.current != null) {
      window.clearTimeout(leaveTimer.current)
      leaveTimer.current = null
    }
  }

  const handleMouseEnter = () => {
    clearLeave()
    setOpen(true)
  }

  const handleMouseLeave = () => {
    clearLeave()
    // 延迟关闭，方便移入小窗
    leaveTimer.current = window.setTimeout(() => setOpen(false), 180)
  }

  const handleClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation()
      // 单击：快速切换 dark/light
      if (resolvedTheme === 'dark') {
        void setTheme('light')
      } else {
        void setTheme('dark')
      }
    },
    [resolvedTheme, setTheme],
  )

  useEffect(() => () => clearLeave(), [])

  const iconCls = compact ? 'h-4 w-4' : 'h-4 w-4'
  const btnCls = compact ? 'h-7 w-7' : 'h-8 w-8'

  return (
    <div
      ref={wrapRef}
      className={cn('relative', className)}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      data-testid="theme-button-wrap"
    >
      <button
        type="button"
        onClick={handleClick}
        className={cn(
          'flex items-center justify-center rounded-md transition-colors',
          'text-muted-foreground hover:text-foreground hover:bg-white/5',
          btnCls,
        )}
        title={
          resolvedTheme === 'dark'
            ? '点击切换浅色 · 悬停选择主题'
            : '点击切换深色 · 悬停选择主题'
        }
        aria-label="切换主题"
        data-testid="theme-button"
      >
        {resolvedTheme === 'dark' ? (
          <Moon className={iconCls} />
        ) : (
          <Sun className={iconCls} />
        )}
      </button>

      <ThemePopover
        open={open}
        onOpenChange={setOpen}
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
      />
    </div>
  )
}
