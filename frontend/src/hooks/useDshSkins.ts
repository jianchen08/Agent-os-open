/**
 * DSH 皮肤清单/选择共享 hook（ThemePopover 浮框 + ThemeSettingsPage 复用）
 *
 * 数据源 = dsh_adapter 插件动态端点（GET /ext/dsh_adapter/skins），数量随
 * 装载的皮肤插件自动增减；选择走 PUT（写回 config 后热生效，刷新页面后
 * 皮肤 CSS 全量换装）。适配器不可用时返回空清单，调用方静默不渲染。
 */

import { useCallback, useEffect, useState } from 'react'

import { App as AntdApp } from 'antd'

import { apiClient } from '@/services/api/client'
import { useThemeStore } from '@/stores/themeStore'

export interface DshSkin {
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
 * 模块级清单缓存：浮框每次悬停都重新挂载组件，不能每次都打端点
 * （用户实测：反复 hover 反复加载）。30s 内复用，之后静默重验证。
 */
interface SkinCache {
  data: DshSkinList
  at: number
}
let skinCache: SkinCache | null = null
let inflight: Promise<DshSkinList> | null = null

const CACHE_TTL_MS = 30_000

function fetchSkinList(force = false): Promise<DshSkinList> {
  if (!force && skinCache && Date.now() - skinCache.at < CACHE_TTL_MS) {
    return Promise.resolve(skinCache.data)
  }
  if (inflight) return inflight
  inflight = apiClient
    .get<DshSkinList>('/ext/dsh_adapter/skins')
    .then((resp) => {
      skinCache = { data: resp.data, at: Date.now() }
      return resp.data
    })
    .finally(() => {
      inflight = null
    })
  return inflight
}

function patchCacheCurrent(skinId: string) {
  if (skinCache) {
    skinCache = { ...skinCache, data: { ...skinCache.data, current: skinId } }
  }
}

/** 即时换装：重拉 skin.css 就地替换 <style> 标签（无需刷新页面）。 */
async function applySkinCssImmediately() {
  try {
    const el = document.querySelector<HTMLStyleElement>('style[data-plugin-style="dsh_adapter:dsh-skin"]')
    if (!el) return
    const resp = await apiClient.get<string>('/ext/dsh_adapter/styles/skin.css', { responseType: 'text' })
    el.textContent = typeof resp.data === 'string' ? resp.data : String(resp.data)
  } catch {
    // 换装失败不阻断（刷新页面仍可兜底）
  }
}

export function useDshSkins() {
  const { message } = AntdApp.useApp()
  const [list, setList] = useState<DshSkinList | null>(skinCache?.data ?? null)
  const [current, setCurrent] = useState<string | null>(skinCache?.data.current ?? null)
  const [switching, setSwitching] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchSkinList()
      .then((data) => {
        if (!cancelled) {
          setList(data)
          setCurrent(data.current)
        }
      })
      .catch(() => {
        // 适配器不可用/未启用：静默（调用方按 count===0 不渲染区块）
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
        patchCacheCurrent(skinId)
        await applySkinCssImmediately()
        // 基准回退规则（用户裁决 2026-08-21）：皮肤 = 整体替换，基准是内置
        // 暗/亮主题（按皮肤 base）而非叠加在当前灵汐主题上——皮肤未覆盖处
        // 回落基准暗/亮。none 时恢复皮肤激活前的用户主题。
        const themeStore = useThemeStore.getState()
        if (skinId === 'none') {
          const prev = localStorage.getItem('dsh-skin-prev-theme')
          if (prev && prev !== themeStore.currentThemeId) {
            void themeStore.setTheme(prev)
          }
          localStorage.removeItem('dsh-skin-prev-theme')
        } else {
          const skin = (list?.skins ?? []).find((s) => s.id === skinId)
          const baseTheme = skin?.base === 'light' ? 'light' : 'dark'
          if (themeStore.currentThemeId !== baseTheme) {
            localStorage.setItem('dsh-skin-prev-theme', themeStore.currentThemeId)
          }
          void themeStore.setTheme(baseTheme)
        }
        void message.success(
          skinId === 'none'
            ? '已关闭 DSH 皮肤，刷新页面后生效'
            : `已切换 DSH 皮肤：${skinId}，刷新页面后完全生效`
        )
      } catch {
        void message.error('DSH 皮肤切换失败（适配器不可用或皮肤 id 无效）')
      } finally {
        setSwitching(null)
      }
    },
    [message, list],
  )

  return { list, current: current ?? 'none', skins: list?.skins ?? [], count: list?.count ?? 0, switching, selectSkin }
}
