/**
 * 主题面板组件
 *
 * 紧凑的主题选择面板，使用网格布局显示所有主题
 */

import { Check, Settings } from 'lucide-react'
import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useThemeStore } from '@/stores/themeStore'
import type { ThemeInfo } from '@/types/theme'

interface ThemePanelProps {
  /** 是否打开面板 */
  isOpen: boolean
  /** 关闭面板回调 */
  onClose?: () => void
}

/**
 * 获取主题预览颜色
 *
 * 优先使用主题自带的预览色，否则根据主题类别返回默认色
 */
function getPreviewColors(theme: ThemeInfo) {
  // 使用预览色或根据类别返回默认色
  if (theme.preview) {
    return {
      bg: theme.preview.background,
      primary: theme.preview.primary,
      text: theme.preview.text,
    }
  }
  // 默认颜色
  if (theme.category === 'light' || theme.id === 'light') {
    return { bg: '#f8fafc', primary: '#2563eb', text: '#0f172a' }
  }
  return { bg: '#0f172a', primary: '#3b82f6', text: '#f8fafc' }
}

/**
 * 紧凑主题卡片
 *
 * 显示主题预览色块和名称，选中时显示勾选标记
 */
function ThemeCard({
  theme,
  isSelected,
  onSelect,
}: {
  theme: ThemeInfo
  isSelected: boolean
  onSelect: () => void
}) {
  const colors = getPreviewColors(theme)

  return (
    <button
      onClick={onSelect}
      className={cn(
        'relative rounded-lg border p-2 transition-all duration-200',
        'hover:border-primary/50 hover:shadow-md',
        isSelected ? 'border-primary ring-primary/20 ring-2' : 'border-border/50',
      )}
      title={theme.description || theme.name}
    >
      {/* 预览色块 */}
      <div
        className="mb-1.5 flex h-8 w-full items-center justify-center rounded"
        style={{ backgroundColor: colors.bg }}
      >
        <div className="h-4 w-4 rounded-full" style={{ backgroundColor: colors.primary }} />
      </div>

      {/* 主题名称 */}
      <div className="flex items-center justify-between gap-1">
        <span className="flex-1 truncate text-xs font-medium">{theme.name}</span>
        {isSelected && <Check className="text-primary h-3 w-3 flex-shrink-0" />}
      </div>
    </button>
  )
}

/**
 * 主题面板主组件
 *
 * 面板打开时自动刷新主题列表，分组显示基础主题和扩展主题
 */
export function ThemePanel({ isOpen, onClose }: ThemePanelProps) {
  const navigate = useNavigate()
  const { currentThemeId, availableThemes, setTheme, refreshThemes } = useThemeStore()

  // 面板打开时刷新主题列表
  useEffect(() => {
    if (isOpen) {
      refreshThemes()
    }
  }, [isOpen, refreshThemes])

  /**
   * 选择主题并关闭面板
   */
  const handleThemeSelect = async (themeId: string) => {
    await setTheme(themeId)
    onClose?.()
  }

  if (!isOpen) {
    return null
  }

  // 默认主题列表
  const defaultThemes: ThemeInfo[] = [
    { id: 'dark', name: '深色', category: 'dark' },
    { id: 'light', name: '浅色', category: 'light' },
  ]

  const themes = availableThemes.length > 0 ? availableThemes : defaultThemes

  // 分组：基础主题和扩展主题
  const basicThemes = themes.filter((t) => t.id === 'dark' || t.id === 'light')
  const extendedThemes = themes.filter((t) => t.id !== 'dark' && t.id !== 'light')

  return (
    <>
      {/* 移动端背景遮罩 */}
      {isOpen && (
        <div
          className="fixed inset-0 z-[99] bg-black/40 md:hidden"
          onClick={onClose}
        />
      )}

      {/* 面板主体：移动端 fixed bottom sheet / 桌面端 absolute dropdown */}
      <div
        className={cn(
          'z-[100] rounded-lg border shadow-xl',
          'fixed inset-x-0 bottom-0 max-h-[70vh] overflow-y-auto md:absolute md:inset-x-auto md:bottom-auto md:right-0 md:top-full md:mt-2 md:max-h-none md:w-72',
        )}
        style={{
          backgroundColor: 'var(--modal-bg, hsl(var(--card)))',
          boxShadow: '0 20px 40px -12px rgba(0, 0, 0, 0.3)',
        }}
      >
      {/* 基础主题 - 浅色/深色快速切换 */}
      <div className="border-border/50 border-b p-3">
        <div className="text-muted-foreground mb-2 text-xs">快速切换</div>
        <div className="grid grid-cols-2 gap-2">
          {basicThemes.map((theme) => (
            <ThemeCard
              key={theme.id}
              theme={theme}
              isSelected={currentThemeId === theme.id}
              onSelect={() => handleThemeSelect(theme.id)}
            />
          ))}
        </div>
      </div>

      {/* 扩展主题 - 网格布局 */}
      {extendedThemes.length > 0 && (
        <div className="p-3">
          <div className="text-muted-foreground mb-2 text-xs">
            更多主题 ({extendedThemes.length})
          </div>
          <div className="grid max-h-48 grid-cols-3 gap-2 overflow-y-auto">
            {extendedThemes.map((theme) => (
              <ThemeCard
                key={theme.id}
                theme={theme}
                isSelected={currentThemeId === theme.id}
                onSelect={() => handleThemeSelect(theme.id)}
              />
            ))}
          </div>
        </div>
      )}

      {/* 底部操作 */}
      <div className="border-border/50 border-t p-2">
        <button
          onClick={() => {
            navigate('/settings')
            onClose?.()
          }}
          className={cn(
            'flex w-full items-center justify-center gap-2 rounded py-2',
            'text-muted-foreground hover:text-foreground text-xs',
            'hover:bg-muted/50 transition-colors',
          )}
        >
          <Settings className="h-3 w-3" />
          自定义主题设置
        </button>
      </div>
      </div>
    </>
  )
}
