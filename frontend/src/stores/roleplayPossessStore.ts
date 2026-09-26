/**
 * 角色扮演附身态（possess 链宿主侧，roleplay.possess 桥协议）
 *
 * roleplay 面板（webview iframe）POST /data/actions/play 且 play_mode="possess"
 * 成功后，经面板桥上行 roleplay.possess 携带 {card_id, name, avatar, personaText?}
 * → 本 store 持有附身档；发送链（router.tsx）按持有态把卡人设并入消息级
 * execution_context（roleplay_persona，见 modeOptions.withRoleplayPersona），
 * 主 agent 身份不变，「解除」即清档。
 *
 * 载荷校验 fail-closed（与 theme.apply 同款语义）：setPossessed 收原始载荷，
 * 校验失败零状态变更并返回 false，由桥回执 error。persist 到 localStorage
 * （键 roleplay.possessed）使宿主重启后附身态可恢复——附身是用户显式选择，
 * 不随刷新丢失。
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { createTolerantStorage } from '@/utils/tolerantStorage'
import type { RoleplayPossession } from '@/services/schema/modeOptions'

/** avatar 合法形态：非空字符串（emoji）或 {fg,bg} 色对（卡画廊同款两形态） */
function parseAvatar(value: unknown): RoleplayPossession['avatar'] | null {
  if (typeof value === 'string' && value.trim()) return value
  if (typeof value === 'object' && value !== null) {
    const c = value as Record<string, unknown>
    if (typeof c.fg === 'string' && typeof c.bg === 'string') return { fg: c.fg, bg: c.bg }
  }
  return null
}

/**
 * 载荷 → 附身档：card_id/name 非空字符串 + avatar emoji/色对两形态为必要项；
 * personaText 可选字符串（缺席/非字符串容缺为空串，不触发整包拒绝——面板是
 * 唯一生产者，附身不因可选装饰字段缺席而失败）。非法返回 null。
 */
export function parsePossessPayload(payload: unknown): RoleplayPossession | null {
  if (typeof payload !== 'object' || payload === null) return null
  const p = payload as Record<string, unknown>
  if (typeof p.card_id !== 'string' || !p.card_id.trim()) return null
  if (typeof p.name !== 'string' || !p.name.trim()) return null
  const avatar = parseAvatar(p.avatar)
  if (!avatar) return null
  const personaText = typeof p.personaText === 'string' ? p.personaText : ''
  return { card_id: p.card_id, name: p.name, avatar, personaText }
}

interface RoleplayPossessState {
  /** 当前附身档；null = 未附身（发送不带附身键） */
  possessed: RoleplayPossession | null
  /** 记附身档（载荷校验失败返回 false 且零状态变更） */
  setPossessed: (payload: unknown) => boolean
  /** 解除附身（发送链随之不带附身键） */
  clearPossessed: () => void
}

export const useRoleplayPossessStore = create<RoleplayPossessState>()(
  persist(
    (set) => ({
      possessed: null,

      setPossessed: (payload) => {
        const possessed = parsePossessPayload(payload)
        if (!possessed) return false
        set({ possessed })
        return true
      },

      clearPossessed: () => set({ possessed: null }),
    }),
    {
      name: 'roleplay.possessed',
      // v1：possessed 增 personaText（卡人设）。v0 旧档无该键，迁移容缺为空串
      // （附身档跨升级连贯，不产生 roleplay_persona 缺键的半失效态）。
      version: 1,
      migrate: (persisted) => {
        const stored = persisted as { possessed?: RoleplayPossession | null }
        if (!stored.possessed) return stored as RoleplayPossessState
        return { possessed: { ...stored.possessed, personaText: stored.possessed.personaText ?? '' } }
      },
      storage: createTolerantStorage(),
      // 仅持久化附身档本体（动作函数不落盘）
      partialize: (state) => ({ possessed: state.possessed }),
    },
  ),
)
