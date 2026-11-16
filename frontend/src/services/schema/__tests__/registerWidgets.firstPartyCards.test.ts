/** @feature FP-RP面板v3.1刀5 | @ci: frontend-test */
/**
 * registerWidgets · 内置首选内容卡集接线测试（ADR §5）
 *
 * first-party 声明全部过 normalizeContentCard fail-closed 校验（与插件声明同一
 * 准入门槛），initializeWidgets 组合根注册一次走同一注册路径（注册序插件段在前
 * 的裁决语义由注册表测试覆盖，此处锁定接线）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { normalizeContentCard } from '@/services/schema/contentCard'
import { FIRST_PARTY_CONTENT_CARDS, initializeWidgets } from '@/services/schema/registerWidgets'

beforeEach(() => {
  contributionRegistry.clear()
})

describe('FIRST_PARTY_CONTENT_CARDS — 内置首选卡集', () => {
  it('html/csv 双卡声明且全部过 fail-closed 校验（同一准入门槛，无豁免）', () => {
    expect(FIRST_PARTY_CONTENT_CARDS.map((c) => c.id).sort()).toEqual(['csv_table', 'html_preview'])
    for (const card of FIRST_PARTY_CONTENT_CARDS) {
      expect(normalizeContentCard(card, card.pluginId)).not.toBeNull()
    }
  })

  it('initializeWidgets 组合根注册 → 经同一注册路径可查（html/csv 命中，插件卡未注册时兜底）', () => {
    const spy = vi.spyOn(contributionRegistry, 'registerFirstPartyContentCards')
    initializeWidgets()
    expect(spy).toHaveBeenCalledWith(FIRST_PARTY_CONTENT_CARDS)
    // 注册后即查即得（渲染层无内置型 if 的前提：查表可达）
    expect(contributionRegistry.getAllContentCards().find((c) => c.id === 'html_preview')).toBeTruthy()
    expect(contributionRegistry.getAllContentCards().find((c) => c.id === 'csv_table')).toBeTruthy()
  })
})
