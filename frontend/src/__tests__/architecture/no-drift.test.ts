/**
 * 架构契约测试：页面漂移禁令（基线单调收敛）
 *
 * 意图：统一审查 §一/§二 列出三类页面漂移（手写外壳/getStatusStyle/原生 a 链接）
 * 与两类统一层零消费（shared/ui/card）。lint 只能 warn 拦新增；本测试把"不恶化"
 * 钉成 CI 失败级约束，并把当前存量记为基线——迁移每完成一页，基线下调、消费上调。
 *
 * 基线快照：docs/working/design/lint-baseline.md（2026-08-12 录入）
 * 关联：frontend-design-unification-execution-plan.md §三 M0.3 / §五 M3
 */

import { describe, expect, it } from 'vitest'
import { listSourceFiles, readSource, scanSourceForPattern, scanSourceForRegex } from './harness'

const PAGES = 'src/pages'
const SRC = 'src'

// 基线快照（M3 完成后收紧至 0；新页面必须用 PageShell，不得回退到手写外壳）
const BASELINE = {
  shellPattern: 0, // 手写外壳页面文件数（M0:24→M1:22→M3:0，已全部收敛到 PageShell）
  getStatusStyle: 0, // getStatusStyle 出现次数（M0:4→M1:0，已全部收敛到 StatusBadge）
  anchorHref: 0, // <a href="/..."> 原生内部导航出现次数（M0:23→M1:21→M3:0，全部走 Link）
}

// 统一层消费下限（迁移单调递增）
const CONSUMPTION_MIN = {
  // M3: 全部页面已接入 shared 组件；54→52 = widget 化 T6/T7 退役
  // ContextWindowSettingsPage/CostSettingsPage（各含 shared 引用，合法递减）
  shared: 52,
  uiCard: 0, // M2 接入后转为下限
}

describe('漂移禁令 —— 当前存量 ≤ 基线（迁移单调收敛）', () => {
  it(`手写外壳页面文件数 ≤ ${BASELINE.shellPattern}（基线）`, () => {
    const files = listSourceFiles(PAGES).filter((f) =>
      readSource(f).includes('flex h-screen flex-col overflow-hidden'),
    )
    expect(files.length, '手写外壳页面数不得增加').toBeLessThanOrEqual(BASELINE.shellPattern)
  })

  it(`getStatusStyle 出现次数 ≤ ${BASELINE.getStatusStyle}（基线）`, () => {
    const hits = scanSourceForPattern('getStatusStyle', listSourceFiles(PAGES))
    expect(hits.length, 'getStatusStyle 不得增加，统一到 shared/StatusBadge').toBeLessThanOrEqual(
      BASELINE.getStatusStyle,
    )
  })

  it(`<a href="/..."> 原生内部导航次数 ≤ ${BASELINE.anchorHref}（基线）`, () => {
    const hits = scanSourceForRegex(/<a href="\/[^"]*"/, listSourceFiles(PAGES))
    expect(hits.length, '禁止新增 <a href="/"> 式整页刷新导航').toBeLessThanOrEqual(
      BASELINE.anchorHref,
    )
  })
})

describe('统一层消费 —— 下限棘轮（消费回退即回归）', () => {
  it(`components/shared 生产引用 ≥ ${CONSUMPTION_MIN.shared}（M3 全量页面已接入）`, () => {
    const hits = scanSourceForPattern('components/shared', listSourceFiles(SRC))
    expect(hits.length, 'shared 层消费不得回退').toBeGreaterThanOrEqual(CONSUMPTION_MIN.shared)
  })

  it('components/ui/card 当前 0 消费（M2 接入后转为下限棘轮）', () => {
    const hits = scanSourceForPattern('components/ui/card', listSourceFiles(SRC))
    expect(hits.length, 'ui/card 消费变化必须是有意的（更新 BASELINE）').toBe(0)
  })
})
