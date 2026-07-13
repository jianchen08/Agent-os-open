/**
 * 主题切换按钮组件
 *
 * 快速切换浅色/深色模式，点击图标直接切换
 * 长按或右键可打开完整主题面板
 */

import { Moon, Sun, Palette } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useThemeStore } from '@/stores/themeStore'

interface ThemeButtonProps {
  /** 点击回调（打开主题面板） */
  onClick?: () => void
}

/**
 * 主题切换按钮组件
 *
 * 左侧按钮快速切换深浅模式，右侧按钮打开更多主题选项
 */
export function ThemeButton({ onClick }: ThemeButtonProps) {
  const { resolvedTheme, setTheme } = useThemeStore()

  /**
   * 快速切换浅色/深色模式
   */
  const handleQuickToggle = (e: React.MouseEvent) => {
    e.stopPropagation()
    // 根据当前解析的主题切换
    if (resolvedTheme === 'dark') {
      setTheme('light')
    } else {
      setTheme('dark')
    }
  }

  return (
    <div className="flex items-center">
      {/* 主题切换按钮 */}
      <button
        onClick={handleQuickToggle}
        className={cn(
          'flex h-8 w-8 items-center justify-center rounded-lg',
          'transition-all duration-200',
          'hover:bg-muted/80 active:scale-95',
          'text-foreground',
        )}
        title={`切换到${resolvedTheme === 'dark' ? '浅色' : '深色'}模式`}
      >
        {resolvedTheme === 'dark' ? <Moon className="h-4 w-4" /> : <Sun className="h-4 w-4" />}
      </button>

      {/* 更多主题选项按钮 */}
      <button
        onClick={onClick}
        className={cn(
          'flex h-8 w-8 items-center justify-center rounded-lg',
          'transition-all duration-200',
          'hover:bg-muted/80 active:scale-95',
          'text-muted-foreground hover:text-foreground',
        )}
        title="更多主题选项"
      >
        <Palette className="h-4 w-4" />
      </button>
    </div>
  )
}
