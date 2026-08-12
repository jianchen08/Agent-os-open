/**
 * 架构契约测试：聊天层图标尺寸 token 收敛（统一审查 §3.2 C2）
 *
 * M4 收敛已完成：h-3/h-3.5/h-4（135 处，12/14/16px）→ h-icon-xs/sm/md（像素等价，零视觉变化）。
 * 剩余 h-5/h-8（33 处，20/32px）无对应 token（不在 12/14/16 阶梯，多为头像/大按钮），
 * 锁为上限棘轮：禁止新增这类散用。
 *
 * 关联：frontend-design-unification-execution-plan.md §七 M4.1
 */

import { describe, expect, it } from 'vitest'
import { listSourceFiles, scanSourceForRegex } from './harness'

const CHAT = 'src/components/chat'

// 上限棘轮：h-3/3.5/4 已全收敛为 token；剩余 h-5/h-8（无 token 等价）锁为上限。
const CHAT_RAW_SIZE_BASELINE = 33

describe('聊天层图标尺寸 —— h-3/3.5/4 已收敛 token，剩余 h-5/h-8 上限棘轮', () => {
  it('chat 组件 h-3/h-3.5/h-4 已全部收敛为 h-icon-xs/sm/md（零残留）', () => {
    const hits = scanSourceForRegex(/h-(?:3\.5|3|4)[^0-9.]/, listSourceFiles(CHAT))
    expect(
      hits.length,
      'h-3/h-3.5/h-4 必须用 h-icon-xs/sm/md（12/14/16px 像素等价）',
    ).toBe(0)
  })

  it(`chat 组件 h-5/h-8（无 token 等价）≤ ${CHAT_RAW_SIZE_BASELINE}，禁止新增`, () => {
    const hits = scanSourceForRegex(/h-(?:5|8)[^0-9.]/, listSourceFiles(CHAT))
    expect(hits.length, 'h-5/h-8 无对应 token，不得新增散用').toBeLessThanOrEqual(
      CHAT_RAW_SIZE_BASELINE,
    )
  })
})
