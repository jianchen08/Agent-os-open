/**
 * 主题设置页面
 *
 * 提供多套预设主题的选择切换，支持 light/dark/system 模式。
 * 主题系统完全前端化，无后端依赖。
 * 另含「DSH 皮肤」区块：清单动态来自 dsh_adapter 装载的皮肤插件
 * （skin-center），选择写回后端 config（skin.css 注入通道），与前端
 * 主题体系并存——数量随装载自动增减，无需改前端。
 */

import { useCallback, useEffect, useState } from 'react'

import { App as AntdApp } from 'antd'

import { PageShell } from '@/components/shared/PageShell'
import { themeList } from '@/config/themes'
import { apiClient } from '@/services/api/client'
import { useThemeStore } from '@/stores/themeStore'
import type { ThemeInfo } from '@/types/theme'

/** DSH 皮肤清单条目（GET /ext/dsh_adapter/skins） */
interface DshSkin {
  id: string
  name: string
  tagline: string
  accent: string
  base: 'light' | 'dark'
  tags: string[]
  has_background_media: boolean
}

interface DshSkinList {
  current: string | null
  count: number
  skins: DshSkin[]
}

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
                mode === 'light'
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

      <DshSkinSection />
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

/** DSH 皮肤区块：清单动态来自 dsh_adapter（装载多少显示多少），选择写回后端 */
function DshSkinSection() {
  const { message } = AntdApp.useApp()
  const [list, setList] = useState<DshSkinList | null>(null)
  const [current, setCurrent] = useState<string | null>(null)
  const [switching, setSwitching] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    apiClient
      .get<DshSkinList>('/ext/dsh_adapter/skins')
      .then((resp) => {
        if (!cancelled) {
          setList(resp.data)
          setCurrent(resp.data.current)
        }
      })
      .catch(() => {
        // 适配器不可用/未启用：区块静默不渲染（主题页其余功能不受影响）
      })
    return () => {
      cancelled = true
    }
  }, [])

  const selectSkin = useCallback(
    async (skinId: string) => {
      setSwitching(skinId)
      try {
        await apiClient.put('/ext/dsh_adapter/skins/current', { skin: skinId })
        setCurrent(skinId)
        void message.success(
          skinId === 'none'
            ? '已关闭 DSH 皮肤，刷新页面后生效'
            : `已切换 DSH 皮肤：${skinId}，刷新页面后完全生效（背景图/字体）`
        )
      } catch {
        void message.error('DSH 皮肤切换失败（适配器不可用或皮肤 id 无效）')
      } finally {
        setSwitching(null)
      }
    },
    [message]
  )

  if (!list || list.count === 0) return null
  const activeSkin = current ?? 'none'

  return (
    <section className="mt-8">
      <h2 className="mb-1 text-sm font-semibold">
        DSH 皮肤<span className="text-muted-foreground ml-2 font-normal">来自 dsh_adapter 插件 · {list.count} 套</span>
      </h2>
      <p className="text-muted-foreground mb-3 text-xs">
        全局 CSS 注入（配色/字体/背景立绘），随装载的皮肤插件自动增减；与上方前端主题可叠加。
      </p>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        <button
          type="button"
          disabled={switching !== null}
          onClick={() => void selectSkin('none')}
          className={`rounded-lg border p-4 text-left transition-all ${
            activeSkin === 'none' ? 'border-primary ring-primary/30 ring-2' : 'hover:border-primary/50 border-border'
          }`}
        >
          <h3 className="text-sm font-semibold">不使用 DSH 皮肤</h3>
          <p className="text-muted-foreground mt-1 text-xs">仅使用上方前端主题</p>
          {activeSkin === 'none' && <span className="text-primary mt-2 inline-block text-xs font-medium">✓ 当前</span>}
        </button>
        {list.skins.map((skin) => (
          <button
            key={skin.id}
            type="button"
            disabled={switching !== null}
            onClick={() => void selectSkin(skin.id)}
            className={`rounded-lg border p-4 text-left transition-all ${
              activeSkin === skin.id
                ? 'border-primary ring-primary/30 ring-2'
                : 'hover:border-primary/50 border-border'
            }`}
          >
            <div className="mb-2 flex items-center gap-2">
              {skin.accent && (
                <span
                  className="h-4 w-4 rounded-full border"
                  style={{ backgroundColor: skin.accent }}
                  title="强调色"
                />
              )}
              {skin.has_background_media && (
                <span className="rounded bg-purple-500/10 px-1.5 py-0.5 text-[10px] text-purple-400">背景图</span>
              )}
              <span
                className={`rounded px-1.5 py-0.5 text-[10px] ${
                  skin.base === 'dark' ? 'bg-status-info/100/10 text-status-info' : 'bg-status-warning/100/10 text-status-warning'
                }`}
              >
                {skin.base === 'dark' ? '深色' : '浅色'}
              </span>
            </div>
            <h3 className="text-sm font-semibold">{skin.name}</h3>
            {skin.tagline && <p className="text-muted-foreground mt-1 line-clamp-2 text-xs">{skin.tagline}</p>}
            <span className="text-muted-foreground mt-2 inline-block rounded bg-[var(--hover-overlay)] px-1.5 py-0.5 font-mono text-[10px]">
              插件 · dsh_adapter
            </span>
            {activeSkin === skin.id && (
              <span className="text-primary mt-2 inline-block text-xs font-medium">✓ 当前</span>
            )}
          </button>
        ))}
      </div>
    </section>
  )
}
