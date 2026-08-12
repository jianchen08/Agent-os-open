/**
 * 架构契约测试：聊天层图标尺寸 token 收敛（统一审查 §3.2 C2）
 *
 * 现状：chat 组件散用 h-3/h-3.5/h-4/h-5/h-8 五种尺寸（168 处）。
 * token 阶梯 --icon-size-xs/sm/md 已就绪（tokens.test.ts 守护）。
 * 本测试把当前散用计数锁为上限棘轮：禁止新增散用，收敛推进时下调基线。
 *
 * 完全收敛需视觉回归测试配套（项目当前无），故分阶段：先锁不恶化，再逐组件替换。
 * 关联：frontend-design-unification-execution-plan.md §七 M4.1
 */

import { describe, expect, it } from 'vitest'
import { listSourceFiles, scanSourceForRegex } from './harness'

const CHAT = 'src/components/chat'

// 基线（2026-08-12）：chat/ 下 h-3/h-3.5/h-4/h-5/h-8 命中数。收敛推进时下调。
const CHAT_RAW_ICON_BASELINE = 168

describe('聊天层图标尺寸 —— 散用上限棘轮（禁止新增）', () => {
  it(`chat 组件 h-3/3.5/4/5/8 散用 ≤ ${CHAT_RAW_ICON_BASELINE}（基线），逐步替换为 h-icon-*`, () => {
    const hits = scanSourceForRegex(
      /h-(?:3\.5|3|4|5|8)[^0-9.]/,
      listSourceFiles(CHAT),
    )
    expect(
      hits.length,
      'chat 层不得新增 h-3/h-3.5/h-4/h-5/h-8 散用；新代码用 h-icon-xs/sm/md',
    ).toBeLessThanOrEqual(CHAT_RAW_ICON_BASELINE)
  })
})
