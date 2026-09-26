/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * presenterProfiles — 呈现档案匹配逻辑测试（纯函数车道）
 *
 * resolvePresenterFromCards 三关核验：
 * - 前缀解析：`mode_roleplay/card_luna` → prefix/cardId 拆分 + 映射端点命中
 * - id 匹配：cards 按 id===cardId 命中 → {name, avatar, origin=模式前缀}
 * - 未命中 null：裸 agent_id（无 `/`）/ 前缀不在映射 / id 不在 cards
 *
 * modePresenterEndpoint 同源核验（数据端点映射单点）。
 */

import { describe, expect, it } from 'vitest'
import {
  modePresenterEndpoint,
  resolvePresenterFromCards,
  type PresenterCard,
} from '@/services/api/presenterProfiles'

const CARDS: PresenterCard[] = [
  { id: 'card_luna', name: '月见', avatar: '🌙' },
  { id: 'card_rin', name: '凛', avatar: { fg: '#fff', bg: '#333' } },
  { id: 'card_bare', name: '素卡' },
]

describe('modePresenterEndpoint — 前缀映射数据端点', () => {
  it('mode_roleplay 前缀命中登记端点', () => {
    expect(modePresenterEndpoint('mode_roleplay/card_luna')).toBe('/ext/mode_roleplay/data/cards')
  })

  it('裸 agent_id 与未登记前缀 → null（走 agents 注册表老路）', () => {
    expect(modePresenterEndpoint('main')).toBeNull()
  })

  it('mode_godot 等家族模式前缀不在映射 → null', () => {
    expect(modePresenterEndpoint('mode_godot/card_x')).toBeNull()
  })
})

describe('resolvePresenterFromCards — 卡目录匹配', () => {
  it('id 命中 → 抽 {name, avatar, origin=模式前缀}（字符串 avatar）', () => {
    expect(resolvePresenterFromCards('mode_roleplay/card_luna', CARDS)).toEqual({
      name: '月见',
      avatar: '🌙',
      origin: 'mode_roleplay',
    })
  })

  it('色对 avatar 原样透传；avatar 缺省归 null（不伪造空值）', () => {
    expect(resolvePresenterFromCards('mode_roleplay/card_rin', CARDS)).toEqual({
      name: '凛',
      avatar: { fg: '#fff', bg: '#333' },
      origin: 'mode_roleplay',
    })
    expect(resolvePresenterFromCards('mode_roleplay/card_bare', CARDS)).toEqual({
      name: '素卡',
      avatar: null,
      origin: 'mode_roleplay',
    })
  })

  it('未命中三态 → null：裸 agent_id / 未登记前缀 / id 不在 cards', () => {
    expect(resolvePresenterFromCards('main', CARDS)).toBeNull()
    expect(resolvePresenterFromCards('mode_godot/card_luna', CARDS)).toBeNull()
    expect(resolvePresenterFromCards('mode_roleplay/card_ghost', CARDS)).toBeNull()
  })

  it('空卡目录 → null（端点可达但无卡）', () => {
    expect(resolvePresenterFromCards('mode_roleplay/card_luna', [])).toBeNull()
  })
})
