/**
 * 架构契约测试：聊天层图标尺寸 token 收敛（统一审查 §3.2 C2）
 *
 * M4 收敛已完成：h-3/h-3.5/h-4（135 处，12/14/16px）→ h-icon-xs/sm/md（像素等价，零视觉变化）。
 * 剩余 h-5/h-8（34 处，20/32px）无对应 token（不在 12/14/16 阶梯，多为头像/大按钮），
 * 锁为上限棘轮：禁止新增这类散用。
 *
 * 关联：frontend-design-unification-execution-plan.md §七 M4.1
 */

import { describe, expect, it } from 'vitest'
import { listSourceFiles, scanSourceForRegex } from './harness'

const CHAT = 'src/components/chat'

// 上限棘轮：h-3/3.5/4 已全收敛为 token；剩余 h-5/h-8（无 token 等价）锁为上限。
// 2026-08 33→34：两个新组件各引入 1 处合理 32px 语义（token 阶梯仅 12/14/16px，
// 替换会改变视觉，属零 token 等价的新增）——
// - ReferenceChip.tsx:66  h-8 w-8（引用附件 32px 缩略图 object-cover）
// - ChatInputActions.tsx:53  h-8（声明驱动输入动作栏 32px 按钮高度）
const CHAT_RAW_SIZE_BASELINE = 34

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
