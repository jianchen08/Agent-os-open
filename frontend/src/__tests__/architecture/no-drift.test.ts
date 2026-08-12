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

// 基线快照（2026-08-12）：迁移推进时下调，禁止上调
const BASELINE = {
  shellPattern: 24, // 含 flex h-screen flex-col overflow-hidden 手写外壳的页面文件数
  getStatusStyle: 4, // getStatusStyle 出现次数（定义+调用）
  anchorHref: 23, // <a href="/..."> 原生内部导航出现次数
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

describe('统一层消费 —— 当前精确快照（基线 0；M1 起接入消费者后转为 ≥ BASELINE）', () => {
  // 基线为 0 时用精确匹配：意外引入消费会被捕获，有意接入则同步更新 BASELINE 与 lint-baseline.md。
  it('components/shared 当前 0 消费（M1 PageShell 接入后此约束切换为下限棘轮）', () => {
    const hits = scanSourceForPattern('components/shared', listSourceFiles(SRC))
    expect(hits.length, 'shared 层消费变化必须是有意的（更新 BASELINE）').toBe(0)
  })

  it('components/ui/card 当前 0 消费（M2 接入后此约束切换为下限棘轮）', () => {
    const hits = scanSourceForPattern('components/ui/card', listSourceFiles(SRC))
    expect(hits.length, 'ui/card 消费变化必须是有意的（更新 BASELINE）').toBe(0)
  })
})
