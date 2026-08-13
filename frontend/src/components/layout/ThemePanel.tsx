/**
 * 主题选择
 *
 * - ThemePanel：兼容旧接口（isOpen/onClose）
 * - ThemePopover：悬停小窗，附着在 ThemeButton 旁
 */

import { Check } from '@/assets/icons'
import { useEffect } from 'react'
import { cn } from '@/lib/utils'
import { useThemeStore } from '@/stores/themeStore'
import type { ThemeInfo } from '@/types/theme'

interface ThemePanelProps {
  isOpen: boolean
  onClose?: () => void
}

function getPreviewColors(theme: ThemeInfo) {
  if (theme.preview) {
    return {
      bg: theme.preview.background,
      primary: theme.preview.primary,
      text: theme.preview.text,
    }
  }
  if (theme.category === 'light' || theme.id === 'light') {
    return { bg: '#f8fafc', primary: '#2563eb', text: '#0f172a' }
  }
  return { bg: '#0f172a', primary: '#3b82f6', text: '#f8fafc' }
}

function ThemeSwatch({
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
      type="button"
      onClick={onSelect}
      className={cn(
        'flex items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors',
        'hover:bg-[var(--hover-overlay)]',
        isSelected && 'bg-[var(--hover-overlay)]',
      )}
      title={theme.description || theme.name}
    >
      <span
        className="relative flex h-5 w-5 shrink-0 items-center justify-center rounded-full border"
        style={{
          backgroundColor: colors.bg,
          borderColor: isSelected ? colors.primary : 'rgba(148,163,184,0.35)',
        }}
      >
        <span
          className="h-2 w-2 rounded-full"
          style={{ backgroundColor: colors.primary }}
        />
        {isSelected && (
          <Check className="absolute -right-0.5 -bottom-0.5 h-2.5 w-2.5 text-[var(--ds-accent-primary,#22D3EE)]" />
        )}
      </span>
      <span className="text-foreground min-w-0 flex-1 truncate text-[12px]">
        {theme.name}
      </span>
    </button>
  )
}

function useThemeList() {
  const { currentThemeId, availableThemes, setTheme, refreshThemes } = useThemeStore()
  useEffect(() => {
    void refreshThemes()
  }, [refreshThemes])

  const defaultThemes: ThemeInfo[] = [
    { id: 'dark', name: '深色', category: 'dark' },
    { id: 'light', name: '浅色', category: 'light' },
  ]
  const themes = availableThemes.length > 0 ? availableThemes : defaultThemes
  return { currentThemeId, setTheme, themes }
}

/**
 * 悬停弹出的紧凑主题选择小窗（锚定 ThemeButton）
 */
export function ThemePopover({
  open,
  onOpenChange,
  onMouseEnter,
  onMouseLeave,
}: {
  open: boolean
  onOpenChange?: (open: boolean) => void
  onMouseEnter?: () => void
  onMouseLeave?: () => void
}) {
  const { currentThemeId, setTheme, themes } = useThemeList()

  if (!open) return null

  return (
    <div
      className="border-border absolute bottom-full left-1/2 z-[100] mb-1.5 w-40 -translate-x-1/2 overflow-hidden rounded-lg border shadow-xl"
      style={{
        background: 'var(--ds-bg-panel, hsl(var(--card)))',
        boxShadow: '0 10px 28px -8px rgba(0,0,0,0.45)',
      }}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      data-testid="theme-popover"
    >
      <div className="text-muted-foreground border-border border-b px-2.5 py-1.5 text-[10px] font-medium tracking-wide">
        选择主题
      </div>
      <div className="max-h-52 overflow-y-auto p-1">
        {themes.map((theme) => (
          <ThemeSwatch
            key={theme.id}
            theme={theme}
            isSelected={currentThemeId === theme.id}
            onSelect={() => {
              void setTheme(theme.id)
              onOpenChange?.(false)
            }}
          />
        ))}
      </div>
    </div>
  )
}

/**
 * 兼容旧 API 的主题面板（全尺寸）
 * 现主要用于移动端或显式打开场景
 */
export function ThemePanel({ isOpen, onClose }: ThemePanelProps) {
  const { currentThemeId, setTheme, themes } = useThemeList()

  if (!isOpen) return null

  return (
    <>
      <div className="fixed inset-0 z-[99] bg-[var(--overlay-bg)]" onClick={onClose} />
      <div
        className="border-border fixed right-3 bottom-14 z-[100] w-56 overflow-hidden rounded-lg border shadow-xl"
        style={{
          background: 'var(--ds-bg-panel, hsl(var(--card)))',
        }}
        data-testid="theme-panel"
      >
        <div className="text-muted-foreground border-border border-b px-3 py-2 text-[11px] font-medium">
          选择主题
        </div>
        <div className="max-h-64 overflow-y-auto p-1.5">
          {themes.map((theme) => (
            <ThemeSwatch
              key={theme.id}
              theme={theme}
              isSelected={currentThemeId === theme.id}
              onSelect={() => {
                void setTheme(theme.id)
                onClose?.()
              }}
            />
          ))}
        </div>
      </div>
    </>
  )
}
