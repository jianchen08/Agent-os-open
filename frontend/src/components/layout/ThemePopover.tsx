/**
 * 主题选择
 *
 * - ThemePopover：悬停小窗，附着在 ThemeButton 旁
 */

import { useEffect } from 'react'
import { Check } from '@/assets/icons'
import { THEME_PREVIEW_FALLBACK } from '@/config/themes'
import { cn } from '@/lib/utils'
import { useThemeStore } from '@/stores/themeStore'
import type { ThemeInfo } from '@/types/theme'

function getPreviewColors(theme: ThemeInfo) {
  if (theme.preview) {
    return {
      bg: theme.preview.background,
      primary: theme.preview.primary,
      text: theme.preview.text,
    }
  }
  if (theme.category === 'light' || theme.id === 'light') {
    return THEME_PREVIEW_FALLBACK.light
  }
  return THEME_PREVIEW_FALLBACK.dark
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
          borderColor: isSelected ? colors.primary : 'var(--border)',
        }}
      >
        <span
          className="h-2 w-2 rounded-full"
          style={{ backgroundColor: colors.primary }}
        />
        {isSelected && (
          <Check className="absolute -right-0.5 -bottom-0.5 h-2.5 w-2.5 text-[var(--ds-accent-primary)]" />
        )}
      </span>
      <span className="text-foreground min-w-0 flex-1 truncate text-[12px]">
        {theme.name}
      </span>
      {/* 插件来源标注（contributes.themes 贡献的主题） */}
      {theme.pluginId && (
        <span
          className="text-muted-foreground shrink-0 rounded bg-[var(--hover-overlay)] px-1 py-0.5 font-mono text-[9px]"
          title={`来源插件: ${theme.pluginId}`}
        >
          {theme.pluginId}
        </span>
      )}
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
        background: 'var(--ds-bg-panel)',
        boxShadow: 'var(--shadow-xl)',
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
