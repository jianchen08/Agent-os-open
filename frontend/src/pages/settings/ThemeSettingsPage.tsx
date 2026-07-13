/**
 * 主题设置页面
 *
 * 提供 7 套预设主题的选择切换，支持 light/dark/system 模式。
 * 主题系统完全前端化，无后端依赖。
 */

import { useThemeStore } from '@/stores/themeStore'
import { themeList } from '@/config/themes'
import type { ThemeInfo } from '@/types/theme'

/**
 * 主题设置页面组件
 */
export function ThemeSettingsPage() {
  const { currentThemeId, mode, setTheme, setMode, resolvedTheme } = useThemeStore()

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/settings" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回设置
        </a>
        <h1 className="ml-4 text-base font-semibold">主题设置</h1>
      </header>

      <main className="flex-1 overflow-y-auto p-6">
        {/* 模式切换 */}
        <section className="mb-8">
          <h2 className="mb-3 text-sm font-semibold">显示模式</h2>
          <div className="flex gap-3">
            {(['light', 'dark', 'system'] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`rounded-lg border px-4 py-2 text-sm transition-colors ${
                  mode === m
                    ? 'bg-primary/10 text-primary border-primary/30'
                    : 'hover:bg-accent/30 border-border'
                }`}
              >
                {m === 'light' ? '浅色' : m === 'dark' ? '深色' : '跟随系统'}
              </button>
            ))}
          </div>
          <p className="text-muted-foreground mt-2 text-xs">
            当前解析为：{resolvedTheme === 'dark' ? '深色' : '浅色'}模式
          </p>
        </section>

        {/* 主题选择 */}
        <section>
          <h2 className="mb-3 text-sm font-semibold">选择主题</h2>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {themeList.map((theme) => (
              <ThemeCard
                key={theme.id}
                theme={theme}
                isActive={currentThemeId === theme.id}
                onSelect={() => setTheme(theme.id)}
              />
            ))}
          </div>
        </section>
      </main>
    </div>
  )
}

/** 主题预览卡片 */
function ThemeCard({
  theme,
  isActive,
  onSelect,
}: {
  theme: ThemeInfo
  isActive: boolean
  onSelect: () => void
}) {
  const preview = theme.preview

  return (
    <button
      onClick={onSelect}
      className={`group rounded-lg border p-4 text-left transition-all ${
        isActive
          ? 'border-primary ring-primary/30 ring-2'
          : 'hover:border-primary/50 border-border'
      }`}
    >
      {/* 色彩预览 */}
      {preview && (
        <div className="mb-3 flex gap-1.5">
          <div
            className="h-6 w-6 rounded-full border"
            style={{ backgroundColor: preview.primary }}
            title="主色"
          />
          <div
            className="h-6 w-6 rounded-full border"
            style={{ backgroundColor: preview.background }}
            title="背景色"
          />
          <div
            className="h-6 w-6 rounded-full border"
            style={{ backgroundColor: preview.surface }}
            title="表面色"
          />
          <div
            className="h-6 w-6 rounded-full border"
            style={{ backgroundColor: preview.accent }}
            title="强调色"
          />
        </div>
      )}

      {/* 主题信息 */}
      <h3 className="text-sm font-semibold">{theme.name}</h3>
      {theme.description && (
        <p className="text-muted-foreground mt-1 text-xs line-clamp-2">{theme.description}</p>
      )}

      {/* 激活标识 */}
      {isActive && (
        <span className="text-primary mt-2 inline-block text-xs font-medium">✓ 当前使用</span>
      )}

      {/* 类别标签 */}
      <span
        className={`mt-2 inline-block rounded px-1.5 py-0.5 text-xs ${
          theme.category === 'light'
            ? 'bg-yellow-500/10 text-yellow-600'
            : theme.category === 'dark'
              ? 'bg-blue-500/10 text-blue-400'
              : 'bg-purple-500/10 text-purple-400'
        }`}
      >
        {theme.category === 'light' ? '浅色' : theme.category === 'dark' ? '深色' : '特殊'}
      </span>
    </button>
  )
}
