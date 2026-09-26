/**
 * 皮肤 hooks 启用确认 store（2026-09-25 准入分级波2）
 *
 * skinRuntime 对 hooks.mjs 文本算 sha256 后判定：首次启用或指纹漂移 →
 * 不执行，载荷挂本 store 等用户确认；确认 → pin 落 localStorage + 重应用
 * 皮肤（hooks 随之运行）；取消 → 仅静态 CSS 生效。挂点组件
 * SkinConsentDialog（App 根全局浮层）消费本 store。
 *
 * 与 skinRuntime 的依赖方向：store 不 import skinRuntime（确认后的重应用由
 * Dialog 调 applyPluginSkin），避免环。
 */

import { create } from 'zustand'
import type { PluginTheme } from '@/types/theme'

export interface SkinConsentPayload {
  theme: PluginTheme & { skin: string }
  /** 皮肤 scope（pluginId:skin） */
  scope: string
  /** 本次 fetch 到的 hooks.mjs 文本 sha256（消费点 pin，锚实际执行载荷） */
  hash: string
  reason: 'first' | 'drift'
  /** 漂移前的旧指纹（展示给用户对比） */
  previous?: string
}

interface SkinConsentState {
  pending: SkinConsentPayload | null
  setPending: (payload: SkinConsentPayload | null) => void
}

export const useSkinConsentStore = create<SkinConsentState>()((set) => ({
  pending: null,
  setPending: (payload) => set({ pending: payload }),
}))
