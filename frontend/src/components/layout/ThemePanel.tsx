/**
 * 主题选择
 *
 * - ThemePanel：兼容旧接口（isOpen/onClose）
 * - ThemePopover：悬停小窗，附着在 ThemeButton 旁
 */

import { useEffect } from 'react'
import { Check } from '@/assets/icons'
import { useDshSkins } from '@/hooks/useDshSkins'
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

/** DSH 皮肤紧凑分组（浮框/面板共用；适配器不可用时整组不渲染） */
function DshSkinRows({ onSelected }: { onSelected?: () => void }) {
  const { current, skins, count, switching, selectSkin } = useDshSkins()
  if (count === 0) return null

  return (
    <>
      <div className="text-muted-foreground border-border border-t px-2.5 py-1.5 text-[10px] font-medium tracking-wide">
        DSH 皮肤（{count}）
      </div>
      <div className="max-h-44 overflow-y-auto px-1 pb-1">
        <button
          type="button"
          disabled={switching !== null}
          onClick={() => {
            void selectSkin('none')
            onSelected?.()
          }}
          className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-[var(--hover-overlay)]"
        >
          <span className="border-border flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[9px]">
            ✕
          </span>
          <span className="text-foreground min-w-0 flex-1 truncate text-[12px]">不使用 DSH 皮肤</span>
          {current === 'none' && (
            <Check className="h-3 w-3 text-[var(--ds-accent-primary,#22D3EE)] shrink-0" />
          )}
        </button>
        {skins.map((skin) => (
          <button
            key={skin.id}
            type="button"
            disabled={switching !== null}
            onClick={() => {
              void selectSkin(skin.id)
              onSelected?.()
            }}
            title={skin.tagline || skin.id}
            className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-[var(--hover-overlay)]"
          >
            <span
              className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border"
              style={{
                backgroundColor: skin.accent || 'transparent',
                borderColor: current === skin.id ? skin.accent : 'rgba(148,163,184,0.35)',
              }}
            />
            <span className="text-foreground min-w-0 flex-1 truncate text-[12px]">{skin.name}</span>
            {skin.has_background_media && (
              <span className="shrink-0 rounded bg-purple-500/10 px-1 py-0.5 text-[9px] text-purple-400">
                图
              </span>
            )}
            {current === skin.id && (
              <Check className="h-3 w-3 text-[var(--ds-accent-primary,#22D3EE)] shrink-0" />
            )}
          </button>
        ))}
      </div>
    </>
  )
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
      <DshSkinRows onSelected={() => onOpenChange?.(false)} />
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
        <div className="px-0.5 pb-0.5">
          <DshSkinRows onSelected={() => onClose?.()} />
        </div>
      </div>
    </>
  )
}
