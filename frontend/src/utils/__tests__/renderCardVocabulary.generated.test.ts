/**
 * renderCardVocabulary.generated.ts 契约测试（AR-M5 卡型单源）。
 *
 * 断可观察行为（输入→输出），不钉生成器内部：
 * - 词汇表 lockstep（删值实验前端随动的运行时镜像）：词表集合 ≡
 *   payload 构造器集合 ∪ {generic}——词表收缩而构造器缺位由 tsc 先红，
 *   本测试运行时兜底；词表扩张而构造器缺位同理；
 * - 词表非单侧：diff（dsh_adapter translator 面）与 form（plugin.json 面）
 *   两声明面独占值都必须在词表（「diff 非单侧值」双面实证的运行时锚）；
 * - 声明校验 fail-closed：词表内卡型声明全数装载，词表外卡型（注入假卡型）
 *   声明即弃、回落数据路由。
 */
import { DSH_RENDER_CARDS, RENDER_CARD_VOCABULARY } from '../renderCardVocabulary.generated'
import * as renderIntent from '../renderIntent'

describe('RENDER_CARD_VOCABULARY — 插件侧声明面生成物契约', () => {
  it('词表非空、无重复、全为小写 snake_case', () => {
    expect(RENDER_CARD_VOCABULARY.length).toBeGreaterThan(0)
    expect(new Set(RENDER_CARD_VOCABULARY).size).toBe(RENDER_CARD_VOCABULARY.length)
    for (const card of RENDER_CARD_VOCABULARY) {
      expect(card).toMatch(/^[a-z][a-z_]*$/)
    }
  })

  it('词表两声明面独占值在位（diff=translator 面，form=plugin.json 面）', () => {
    expect(RENDER_CARD_VOCABULARY).toContain('diff')
    expect(RENDER_CARD_VOCABULARY).toContain('form')
  })

  it('lockstep：词表集合 ≡ payload 构造器集合 ∪ {generic}（删/增真值前端随动）', () => {
    const builders = Object.keys(renderIntent.PAYLOAD_BUILDERS)
    expect(new Set(builders)).toEqual(
      new Set(RENDER_CARD_VOCABULARY.filter((c) => c !== 'generic')),
    )
  })
})

describe('DSH_RENDER_CARDS — dshAdapter 消费的 DSH 面子集契约（AR-M5 遗留②）', () => {
  it('非空、无重复、字典序、全为小写 snake_case', () => {
    expect(DSH_RENDER_CARDS.length).toBeGreaterThan(0)
    expect(new Set(DSH_RENDER_CARDS).size).toBe(DSH_RENDER_CARDS.length)
    expect([...DSH_RENDER_CARDS]).toEqual([...DSH_RENDER_CARDS].sort())
    for (const card of DSH_RENDER_CARDS) {
      expect(card).toMatch(/^[a-z][a-z_]*$/)
    }
  })

  it('子集性质：每个值都在全量词表内（面投影不出界）', () => {
    expect(DSH_RENDER_CARDS.length).toBeLessThan(RENDER_CARD_VOCABULARY.length)
    for (const card of DSH_RENDER_CARDS) {
      expect(RENDER_CARD_VOCABULARY).toContain(card)
    }
  })

  it('面边界：translator 面独占 diff 在位，plugin 面独占 form 不在 DSH 面', () => {
    expect(DSH_RENDER_CARDS).toContain('diff')
    expect(DSH_RENDER_CARDS).not.toContain('form')
  })
})

describe('normalizeRenderIntent — 声明校验对齐生成词表', () => {
  it('词表内每个卡型声明都装载成功（词表驱动，非手抄清单）', () => {
    renderIntent.loadRenderIntents(
      RENDER_CARD_VOCABULARY.map((card) => ({ name: `tool_${card}`, render: { card } })),
    )
    for (const card of RENDER_CARD_VOCABULARY) {
      expect(renderIntent.getRenderIntent(`tool_${card}`)?.card).toBe(card)
    }
  })

  it('词表外假卡型声明即弃（fail-closed，回落数据路由）', () => {
    renderIntent.loadRenderIntents([
      { name: 'bad_tool', render: { card: 'hologram' } },
      { name: 'good_tool', render: { card: 'table' } },
    ])
    expect(renderIntent.getRenderIntent('bad_tool')).toBeUndefined()
    expect(renderIntent.getRenderIntent('good_tool')?.card).toBe('table')
  })

  it('addRenderIntent 同词表口径（dshAdapter 贡献装载路径）', () => {
    renderIntent.addRenderIntent('dsh_edit', { card: 'diff' })
    expect(renderIntent.getRenderIntent('dsh_edit')?.card).toBe('diff')
  })
})
