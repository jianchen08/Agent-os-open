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

export function useDshSkins() {
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
    [message],
  )

  return { list, current: current ?? 'none', skins: list?.skins ?? [], count: list?.count ?? 0, switching, selectSkin }
}
