/**
 * content_cards 契约纯逻辑与注册表旁路测试（ADR 2026-10-14-content-card-registry）。
 *
 * 分层：本件只验数据面（契约校验 / fence 匹配裁决 / 注册表双段语义），
 * 渲染面见 ContentCardRenderer / ContentCardGate 测试件。
 */

import { beforeEach, describe, expect, it } from 'vitest'
import {
  CONTENT_CARD_DEFAULT_ORDER,
  extractFenceLanguage,
  matchContentCard,
  normalizeContentCard,
  type ContentCardDeclaration,
} from '@/services/schema/contentCard'
import { ContributionRegistry } from '@/services/schema/ContributionRegistry'

function makeCard(overrides: Partial<ContentCardDeclaration> = {}): ContentCardDeclaration {
  return {
    id: 'card_a',
    match: { fences: ['html'] },
    form: 'webview',
    pluginId: 'host',
    ...overrides,
  }
}

describe('contentCard — extractFenceLanguage', () => {
  it('从 language-* 类名提取并归一小写', () => {
    expect(extractFenceLanguage('language-HTML')).toBe('html')
    expect(extractFenceLanguage('language-csv extra-class')).toBe('csv')
  })

  it('无 language-* 类（裸围栏/行内 code）→ null（不参与匹配）', () => {
    expect(extractFenceLanguage('hljs js-highlight')).toBeNull()
    expect(extractFenceLanguage(undefined)).toBeNull()
    expect(extractFenceLanguage('')).toBeNull()
  })

  it('≥2 组区分度：语言名含符号（c++/c#）与普通名同样提取', () => {
    expect(extractFenceLanguage('language-c++')).toBe('c++')
    expect(extractFenceLanguage('language-c#')).toBe('c#')
  })
})

describe('contentCard — normalizeContentCard（fail-closed 校验）', () => {
  it('合法声明归一：fences 小写去重、可选键类型收窄', () => {
    const card = normalizeContentCard(
      { id: 'x', match: { fences: ['HTML', 'html', 'htm'] }, form: 'webview', title: '网页', default_collapsed: true, order: 7, css: '.x{}' },
      'plug',
    )
    expect(card).toEqual({
      id: 'x',
      match: { fences: ['html', 'htm'] },
      form: 'webview',
      pluginId: 'plug',
      title: '网页',
      default_collapsed: true,
      order: 7,
      css: '.x{}',
    })
  })

  it('非法条目剔除：缺 id / fences 空 / form 非法 / template 形态缺 template', () => {
    expect(normalizeContentCard({ match: { fences: ['html'] }, form: 'webview' }, 'p')).toBeNull()
    expect(normalizeContentCard({ id: 'x', match: { fences: [] }, form: 'webview' }, 'p')).toBeNull()
    expect(normalizeContentCard({ id: 'x', match: { fences: ['html'] }, form: 'react' }, 'p')).toBeNull()
    expect(normalizeContentCard({ id: 'x', match: { fences: ['csv'] }, form: 'template' }, 'p')).toBeNull()
    expect(normalizeContentCard({ id: 'x', match: { fences: ['html'] }, form: 'webview', order: 'fast' }, 'p')?.order).toBeUndefined()
  })
})

describe('contentCard — matchContentCard（分发裁决）', () => {
  it('命中声明的 fence → 返回该卡；未命中/无语言 → null（未命中零变化的查询面）', () => {
    const cards = [makeCard()]
    expect(matchContentCard(cards, 'html')?.id).toBe('card_a')
    expect(matchContentCard(cards, 'csv')).toBeNull()
    expect(matchContentCard(cards, null)).toBeNull()
    expect(matchContentCard([], 'html')).toBeNull()
  })

  it('order 小者胜；order 缺省按 CONTENT_CARD_DEFAULT_ORDER', () => {
    const cards = [
      makeCard({ id: 'default_order', match: { fences: ['html'] } }),
      makeCard({ id: 'lower_order', match: { fences: ['html'] }, order: 1 }),
    ]
    expect(matchContentCard(cards, 'html')?.id).toBe('lower_order')
    expect(CONTENT_CARD_DEFAULT_ORDER).toBe(100)
  })

  it('同 order 按数组序（注册序）取首个——插件段在前即插件覆盖内置的裁决基础', () => {
    const cards = [
      makeCard({ id: 'plugin_card', pluginId: 'plug', order: 100 }),
      makeCard({ id: 'host_card', pluginId: 'host', order: 100 }),
    ]
    expect(matchContentCard(cards, 'html')?.id).toBe('plugin_card')
  })

  it('fence 大小写在匹配端同样归一（声明端归一 + 查询端归一双保险）', () => {
    expect(matchContentCard([makeCard()], 'HTML')?.id).toBe('card_a')
  })
})

describe('ContributionRegistry — content_cards 旁路注册', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('schema 声明注册：合法条目入插件段，与 first-party 拼接序查询', () => {
    registry.registerFirstPartyContentCards([makeCard({ id: 'host_html', pluginId: 'host' })])
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'viewer',
          contributes: { content_cards: [{ id: 'viewer_html', match: { fences: ['html'] }, form: 'webview' }] },
        },
      ],
    })
    const all = registry.getAllContentCards()
    expect(all).toHaveLength(2)
    expect(all[0]).toMatchObject({ id: 'viewer_html', pluginId: 'viewer' })
    expect(all[1]).toMatchObject({ id: 'host_html', pluginId: 'host' })
    // 同 order 插件段在前 → 插件覆盖内置（ADR §2 裁决的可观察行为）
    expect(matchContentCard(registry.getAllContentCards(), 'html')?.id).toBe('viewer_html')
  })

  it('非法声明剔除不注册；同 pluginId+id 幂等覆盖', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'viewer',
          contributes: {
            content_cards: [
              { id: 'bad', match: { fences: [] }, form: 'webview' },
              { id: 'good', match: { fences: ['csv'] }, form: 'template', template: '{{csv_table}}' },
              { id: 'good', match: { fences: ['csv', 'tsv'] }, form: 'template', template: '{{csv_table}}' },
            ],
          },
        },
      ],
    })
    const all = registry.getAllContentCards()
    expect(all).toHaveLength(1)
    expect(all[0].match.fences).toEqual(['csv', 'tsv'])
  })

  it('loadFromSchema 幂等重载清插件段、保留 first-party 段（registerPresetWidgets 同款语义）', () => {
    registry.registerFirstPartyContentCards([makeCard({ id: 'host_csv', match: { fences: ['csv'] }, form: 'template', template: '{{csv_table}}' })])
    registry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'viewer', contributes: { content_cards: [{ id: 'v', match: { fences: ['html'] }, form: 'webview' }] } },
      ],
    })
    expect(registry.getAllContentCards()).toHaveLength(2)

    registry.loadFromSchema({})
    expect(registry.getAllContentCards()).toHaveLength(1)
    expect(registry.getAllContentCards()[0].id).toBe('host_csv')
  })

  it('分层边界：content_cards 与 message_cards 同 contributes 声明互不干扰、各入各表', () => {
    registry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'mixed',
          contributes: {
            content_cards: [{ id: 'cc', match: { fences: ['html'] }, form: 'webview' }],
            message_cards: [{ match: { kind: 'bubble' }, style_id: 'mc' }],
          },
        },
      ],
    })
    expect(registry.getAllContentCards()).toHaveLength(1)
    expect(registry.getAllMessageCards()).toHaveLength(1)
    // content_cards 不进 pages 归一化（旁路键，非页面垃圾卡）
    expect(registry.getPluginPages('mixed')).toHaveLength(0)
  })
})
