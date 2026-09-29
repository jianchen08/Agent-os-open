/**
 * 人设接管附身态（persona 接管链宿主侧，mode.possess 桥协议，D7 通用机制）
 *
 * 模式面板（webview iframe）附身动作成功后，经面板桥上行 mode.possess 携带
 * {mode, card_id, name, avatar, personaText?} → 本 store 持有附身档；人设注入
 * 键（personaKey）在建立时从 registry 该模式声明 decl.persona.from 解析一次
 * 随档钉住（出生即钉，D9 同形态）——发送链（router.tsx）按持有态把人设并入
 * 消息级 execution_context（execution_context[personaKey] + mode=附身模式键，
 * 见 modeOptions.withPossession），主 agent 身份不变，「解除」即清档。
 *
 * 载荷校验 fail-closed（与 theme.apply 同款语义）：setPossessed 收原始载荷 +
 * 已解析注入键，校验失败零状态变更并返回 false，由桥回执 error。persist 到
 * localStorage（键 persona.possessed）使宿主重启后附身态可恢复——附身是用户
 * 显式选择，不随刷新丢失。旧 roleplay 专属档（键 roleplay.possessed）一次性
 * 迁移补 mode='roleplay'（personaKey 同步钉旧契约键，数据面兼容非逻辑分支）。
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { createTolerantStorage } from '@/utils/tolerantStorage'
import { isModeIdShape, type PersonaPossession } from '@/services/schema/modeOptions'

/** avatar 合法形态：非空字符串（emoji）或 {fg,bg} 色对（卡画廊同款两形态） */
function parseAvatar(value: unknown): PersonaPossession['avatar'] | null {
  if (typeof value === 'string' && value.trim()) return value
  if (typeof value === 'object' && value !== null) {
    const c = value as Record<string, unknown>
    if (typeof c.fg === 'string' && typeof c.bg === 'string') return { fg: c.fg, bg: c.bg }
  }
  return null
}

/**
 * 载荷 → 附身档（不含注入键）：mode 键形态守卫 + card_id/name 非空字符串 +
 * avatar emoji/色对两形态为必要项；personaText 可选字符串（缺席/非字符串容缺
 * 为空串，不触发整包拒绝——面板是唯一生产者，附身不因可选装饰字段缺席而
 * 失败）。非法返回 null。
 */
export function parsePossessPayload(
  payload: unknown,
): Omit<PersonaPossession, 'personaKey'> | null {
  if (typeof payload !== 'object' || payload === null) return null
  const p = payload as Record<string, unknown>
  if (!isModeIdShape(p.mode)) return null
  if (typeof p.card_id !== 'string' || !p.card_id.trim()) return null
  if (typeof p.name !== 'string' || !p.name.trim()) return null
  const avatar = parseAvatar(p.avatar)
  if (!avatar) return null
  const personaText = typeof p.personaText === 'string' ? p.personaText : ''
  return { mode: p.mode, card_id: p.card_id, name: p.name, avatar, personaText }
}

interface PersonaPossessState {
  /** 当前附身档；null = 未附身（发送不带附身键） */
  possessed: PersonaPossession | null
  /** 记附身档（载荷或注入键校验失败返回 false 且零状态变更；personaKey=null
   *  = registry 不可达未解析 → 诚实降级：发送链不注入附身） */
  setPossessed: (payload: unknown, personaKey: string | null) => boolean
  /** 解除附身（发送链随之不带附身键） */
  clearPossessed: () => void
}

/**
 * 旧 roleplay 专属档一次性迁移（roleplay.possessed → persona.possessed）：
 * 迁移补 mode='roleplay' + personaKey 钉旧契约键 roleplay_persona（历史数据面
 * 兼容——字面量只出现在迁移 shim，非逻辑分支）；幂等（新档在场/旧键缺席零
 * 动作），迁移后旧键清除。store 创建前执行（persist 同步水合读新键）。
 */
function migrateLegacyRoleplayPossession(): void {
  try {
    const legacyRaw = localStorage.getItem('roleplay.possessed')
    if (!legacyRaw) return
    const nextRaw = localStorage.getItem('persona.possessed')
    const next = nextRaw
      ? (JSON.parse(nextRaw) as { state?: { possessed?: unknown } })
      : null
    if (next?.state?.possessed) {
      localStorage.removeItem('roleplay.possessed')
      return
    }
    const legacy = JSON.parse(legacyRaw) as {
      state?: { possessed?: Record<string, unknown> } | null
    }
    const old = legacy?.state?.possessed
    if (!old) {
      localStorage.removeItem('roleplay.possessed')
      return
    }
    localStorage.setItem(
      'persona.possessed',
      JSON.stringify({
        state: {
          possessed: {
            mode: 'roleplay',
            card_id: old.card_id ?? '',
            name: old.name ?? '',
            avatar: old.avatar ?? '',
            personaText: old.personaText ?? '',
            personaKey: 'roleplay_persona',
          },
        },
        version: 1,
      }),
    )
    localStorage.removeItem('roleplay.possessed')
  } catch {
    // 迁移失败（损坏档/存储禁用）零阻断：persist 走无档初始化（未附身）
  }
}

migrateLegacyRoleplayPossession()

export const usePersonaPossessStore = create<PersonaPossessState>()(
  persist(
    (set) => ({
      possessed: null,

      setPossessed: (payload, personaKey) => {
        const base = parsePossessPayload(payload)
        if (!base) return false
        if (personaKey !== null && !personaKey.trim()) return false
        set({ possessed: { ...base, personaKey } })
        return true
      },

      clearPossessed: () => set({ possessed: null }),
    }),
    {
      name: 'persona.possessed',
      version: 1,
      storage: createTolerantStorage(),
      // 仅持久化附身档本体（动作函数不落盘）
      partialize: (state) => ({ possessed: state.possessed }),
    },
  ),
)
