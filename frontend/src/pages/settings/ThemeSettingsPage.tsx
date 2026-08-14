/**
 * 主题设置页面
 *
 * 提供多套预设主题的选择切换，支持 light/dark/system 模式。
 * 主题系统完全前端化，无后端依赖。
 */

import { PageShell } from '@/components/shared/PageShell'
import { themeList } from '@/config/themes'
import { useThemeStore } from '@/stores/themeStore'
import type { ThemeInfo } from '@/types/theme'

/**
 * 主题设置页面组件
 *
 * @param embedded 嵌入设置主页右侧面板时为 true（去掉独立全屏头）
 */
export function ThemeSettingsPage({ embedded = false }: { embedded?: boolean }) {
  const { currentThemeId, mode, setTheme, setMode, resolvedTheme, availableThemes, refreshThemes } =
    useThemeStore()
  // 与 ThemePanel 同源：优先 store 聚合列表（预设 + 插件贡献 + 用户自定义），
  // store 未初始化时回退静态 themeList（避免首帧空白）。
  const themes = availableThemes.length > 0 ? availableThemes : themeList

  const content = (
    <>
      <section className="mb-8">
        <h2 className="mb-3 text-sm font-semibold">显示模式</h2>
        <div className="flex gap-3">
          {(['light', 'dark', 'system'] as const).map((m) => (
            <button
              key={m}
              type="button"
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

      <section>
        <h2 className="mb-3 text-sm font-semibold">选择主题</h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {themes.map((theme) => (
            <ThemeCard
              key={theme.id}
              theme={theme}
              isActive={currentThemeId === theme.id}
              onSelect={() => {
                void setTheme(theme.id)
                void refreshThemes()
              }}
            />
          ))}
        </div>
      </section>
    </>
  )

  if (embedded) {
    return (
      <PageShell title="主题设置" embedded>
        {content}
      </PageShell>
    )
  }

  return (
    <PageShell title="主题设置" backHref="/settings" backLabel="返回设置">
      {content}
    </PageShell>
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
      type="button"
      onClick={onSelect}
      className={`group rounded-lg border p-4 text-left transition-all ${
        isActive
          ? 'border-primary ring-primary/30 ring-2'
          : 'hover:border-primary/50 border-border'
      }`}
    >
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

      <h3 className="text-sm font-semibold">{theme.name}</h3>
      {theme.description && (
        <p className="text-muted-foreground mt-1 line-clamp-2 text-xs">{theme.description}</p>
      )}

      {/* 插件贡献的主题：标注来源插件 */}
      {theme.pluginId && (
        <span
          className="text-muted-foreground mt-2 inline-block rounded bg-[var(--hover-overlay)] px-1.5 py-0.5 font-mono text-[10px]"
          title={`由插件 ${theme.pluginId} 贡献`}
        >
          插件 · {theme.pluginId}
        </span>
      )}

      {isActive && (
        <span className="text-primary mt-2 inline-block text-xs font-medium">✓ 当前使用</span>
      )}

      <span
        className={`mt-2 inline-block rounded px-1.5 py-0.5 text-xs ${
          theme.category === 'light'
            ? 'bg-status-warning/100/10 text-status-warning'
            : theme.category === 'dark'
              ? 'bg-status-info/100/10 text-status-info'
              : 'bg-purple-500/10 text-purple-400'
        }`}
      >
        {theme.category === 'light' ? '浅色' : theme.category === 'dark' ? '深色' : '特殊'}
      </span>
    </button>
  )
}
