/**
 * 主题面板组件
 *
 * 紧凑的主题选择面板，使用网格布局显示所有主题
 */

import { cn } from '@/lib/utils'
import { useThemeStore } from '@/stores/themeStore'
import type { ThemeInfo } from '@/types/theme'
import { Check, Settings } from 'lucide-react'
import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'

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
        'relative p-2 rounded-lg border transition-all duration-200',
        'hover:border-primary/50 hover:shadow-md',
        isSelected
          ? 'border-primary ring-2 ring-primary/20'
          : 'border-border/50'
      )}
      title={theme.description || theme.name}
    >
      {/* 预览色块 */}
      <div
        className="w-full h-8 rounded mb-1.5 flex items-center justify-center"
        style={{ backgroundColor: colors.bg }}
      >
        <div
          className="w-4 h-4 rounded-full"
          style={{ backgroundColor: colors.primary }}
        />
      </div>

      {/* 主题名称 */}
      <div className="flex items-center justify-between gap-1">
        <span className="text-xs font-medium truncate flex-1">
          {theme.name}
        </span>
        {isSelected && (
          <Check className="w-3 h-3 text-primary flex-shrink-0" />
        )}
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
  const { currentThemeId, availableThemes, setTheme, refreshThemes } =
    useThemeStore()

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
  const basicThemes = themes.filter(t => t.id === 'dark' || t.id === 'light')
  const extendedThemes = themes.filter(
    t => t.id !== 'dark' && t.id !== 'light'
  )

  return (
    <div
      className="absolute right-0 top-full mt-2 w-72 bg-card text-card-foreground border rounded-lg shadow-xl z-50"
      style={{
        backgroundColor: 'var(--modal-bg, hsl(var(--card)))',
        boxShadow: '0 20px 40px -12px rgba(0, 0, 0, 0.3)',
      }}
    >
      {/* 基础主题 - 浅色/深色快速切换 */}
      <div className="p-3 border-b border-border/50">
        <div className="text-xs text-muted-foreground mb-2">快速切换</div>
        <div className="grid grid-cols-2 gap-2">
          {basicThemes.map(theme => (
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
          <div className="text-xs text-muted-foreground mb-2">
            更多主题 ({extendedThemes.length})
          </div>
          <div className="grid grid-cols-3 gap-2 max-h-48 overflow-y-auto">
            {extendedThemes.map(theme => (
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
      <div className="p-2 border-t border-border/50">
        <button
          onClick={() => {
            navigate('/settings')
            onClose?.()
          }}
          className={cn(
            'w-full flex items-center justify-center gap-2 py-2 rounded',
            'text-xs text-muted-foreground hover:text-foreground',
            'hover:bg-muted/50 transition-colors'
          )}
        >
          <Settings className="w-3 h-3" />
          自定义主题设置
        </button>
      </div>
    </div>
  )
}
