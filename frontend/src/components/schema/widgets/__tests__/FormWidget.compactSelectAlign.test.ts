/** @feature FP-T12 前端适配 @ci: frontend-test
 * CompactSelectToggle 下拉对齐 clamp 策略（移动端视觉 P0-2，MuMu 实测 shots/05）：
 * 靠右触发器的下拉浮窗 align=start 向右展开会溢出视口右缘——定位策略须含
 * 视口 clamp：右空间放得下向右（桌面常态）、否则左空间放得下向左翻转、两侧
 * 都放不下取剩余空间大的一侧（radix collision 兜底收进视口）。
 */
import { describe, expect, it } from 'vitest'
import { compactSelectAlign } from '../compactSelectLayout'

describe('compactSelectAlign（下拉对齐视口 clamp，P0-2）', () => {
  const MENU = 192
  const MARGIN = 12

  it('触发器靠左、右空间充足：start（桌面常态向右展开）', () => {
    // 1280 视口，触发器右缘 300：右空间 968 ≥ 192
    expect(compactSelectAlign(1280, 300)).toBe('start')
  })

  it('触发器靠右、右空间不足但左空间充足：end（翻转向左展开，MuMu shots/05 场景）', () => {
    // 360 视口，🧠 触发器右缘 ≈245：右空间 103 < 192，左空间 233 ≥ 192
    expect(compactSelectAlign(360, 245)).toBe('end')
    // 375 视口同构场景
    expect(compactSelectAlign(375, 260)).toBe('end')
  })

  it('clamp 边界：右空间恰好等于菜单宽 → start；差 1px → 大侧兜底（本例右仍大）→ start', () => {
    // 右空间 = 360-12-156 = 192 恰好放下
    expect(compactSelectAlign(360, 156)).toBe('start')
    // 右空间 = 360-12-157 = 191 差 1px 放不下、左空间 145 也放不下 →
    // 兜底取大侧（右 191 > 左 145）→ start，radix collision 收进视口
    expect(compactSelectAlign(360, 157)).toBe('start')
  })

  it('两侧都放不下：取剩余空间大的一侧（radix collision 兜底 clamp）', () => {
    // 极窄视口 200：触发器右缘 30（右空间 158 > 左空间 18→右大）→ start
    expect(compactSelectAlign(200, 30)).toBe('start')
    // 触发器右缘 190（右空间 -2 < 左空间 178→左大）→ end
    expect(compactSelectAlign(200, 190)).toBe('end')
  })

  it('菜单宽/裕度参数可覆盖（字面值性质断言）', () => {
    // 宽菜单 300：360 视口触发器右缘 245（右空间 103、左空间 233）→ end
    expect(compactSelectAlign(360, 245, 300, MARGIN)).toBe('end')
    // 裕度加大到 40：右空间 164、左空间 116 都放不下 → 兜底取大侧 → start
    expect(compactSelectAlign(360, 156, MENU, 40)).toBe('start')
  })
})
