/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * presenterProfiles + modes registry — 呈现档案与声明派生测试（纯函数车道）
 *
 * resolvePresenterFromCards 三关核验（数据源映射参数化，声明派生）：
 * - 前缀解析：`mode_roleplay/card_luna` → prefix/cardId 拆分 + 映射端点命中
 * - id 匹配：cards 按 id===cardId 命中 → {name, avatar, origin=模式前缀}
 * - 未命中 null：裸 agent_id（无 `/`）/ 前缀不在映射 / id 不在 cards
 *
 * presenterSourcesFromModes：mode.yaml presenter 声明 → 数据端点映射
 * （data_cards 才产出；agent_registry/none 无插件数据端点）——硬编码
 * MODE_PRESENTER_SOURCES 已退役（2026-09-28 设计 D5 声明化）。
 */

import { describe, expect, it } from 'vitest'
import {
  modePresenterEndpoint,
  resolvePresenterFromCards,
  type PresenterCard,
} from '@/services/api/presenterProfiles'
import {
  modePresenterEndpointOf,
  presenterSourcesFromModes,
  type ModeDeclaration,
} from '@/services/api/modes'

const SOURCES: Record<string, string> = {
  mode_roleplay: '/ext/mode_roleplay/data/cards',
}

const CARDS: PresenterCard[] = [
  { id: 'card_luna', name: '月见', avatar: '🌙' },
  { id: 'card_rin', name: '凛', avatar: { fg: '#fff', bg: '#333' } },
  { id: 'card_bare', name: '素卡' },
]

function declOf(partial: Partial<ModeDeclaration>): ModeDeclaration {
  return {
    mode: 'x',
    name: 'X',
    pipelines: [],
    presenter: { source: 'none' },
    tool_card: 'native',
    plugin_id: 'mode_x',
    ...partial,
  }
}

describe('presenterSourcesFromModes — 声明派生数据源映射', () => {
  it('data_cards 声明 → /ext/{plugin_id}/data/cards', () => {
    const res = {
      modes: [declOf({ mode: 'roleplay', plugin_id: 'mode_roleplay', presenter: { source: 'data_cards' } })],
      total: 1,
      errors: [],
    }
    expect(presenterSourcesFromModes(res)).toEqual({
      mode_roleplay: '/ext/mode_roleplay/data/cards',
    })
  })

  it('agent_registry/none 声明不产出端点；undefined 响应 → 空映射', () => {
    const res = {
      modes: [
        declOf({ mode: 'a', plugin_id: 'mode_a', presenter: { source: 'agent_registry' } }),
        declOf({ mode: 'b', plugin_id: 'mode_b', presenter: { source: 'none' } }),
      ],
      total: 2,
      errors: [],
    }
    expect(presenterSourcesFromModes(res)).toEqual({})
    expect(presenterSourcesFromModes(undefined)).toEqual({})
  })

  it('modePresenterEndpointOf 逐条声明同源', () => {
    expect(modePresenterEndpointOf(declOf({ plugin_id: 'm', presenter: { source: 'data_cards' } }))).toBe(
      '/ext/m/data/cards',
    )
    expect(modePresenterEndpointOf(declOf({ presenter: { source: 'none' } }))).toBeNull()
  })
})

describe('modePresenterEndpoint — 前缀映射数据端点', () => {
  it('mode_roleplay 前缀命中登记端点', () => {
    expect(modePresenterEndpoint('mode_roleplay/card_luna', SOURCES)).toBe('/ext/mode_roleplay/data/cards')
  })

  it('裸 agent_id 与未登记前缀 → null（走 agents 注册表老路）', () => {
    expect(modePresenterEndpoint('main', SOURCES)).toBeNull()
  })

  it('mode_godot 等家族模式前缀不在映射 → null', () => {
    expect(modePresenterEndpoint('mode_godot/card_x', SOURCES)).toBeNull()
  })
})

describe('resolvePresenterFromCards — 卡目录匹配', () => {
  it('id 命中 → 抽 {name, avatar, origin=模式前缀}（字符串 avatar）', () => {
    expect(resolvePresenterFromCards('mode_roleplay/card_luna', CARDS, SOURCES)).toEqual({
      name: '月见',
      avatar: '🌙',
      origin: 'mode_roleplay',
    })
  })

  it('色对 avatar 原样透传；avatar 缺省归 null（不伪造空值）', () => {
    expect(resolvePresenterFromCards('mode_roleplay/card_rin', CARDS, SOURCES)).toEqual({
      name: '凛',
      avatar: { fg: '#fff', bg: '#333' },
      origin: 'mode_roleplay',
    })
    expect(resolvePresenterFromCards('mode_roleplay/card_bare', CARDS, SOURCES)).toEqual({
      name: '素卡',
      avatar: null,
      origin: 'mode_roleplay',
    })
  })

  it('未命中三态 → null：裸 agent_id / 未登记前缀 / id 不在 cards', () => {
    expect(resolvePresenterFromCards('main', CARDS, SOURCES)).toBeNull()
    expect(resolvePresenterFromCards('mode_godot/card_luna', CARDS, SOURCES)).toBeNull()
    expect(resolvePresenterFromCards('mode_roleplay/card_ghost', CARDS, SOURCES)).toBeNull()
  })

  it('空卡目录 → null（端点可达但无卡）', () => {
    expect(resolvePresenterFromCards('mode_roleplay/card_luna', [], SOURCES)).toBeNull()
  })
})
