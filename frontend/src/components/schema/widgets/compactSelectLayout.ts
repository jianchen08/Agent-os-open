/**
 * 紧凑下拉（CompactSelectToggle）定位策略单源。
 *
 * 独立成文件的原因：FormWidget.tsx 是组件文件，react-refresh/only-export-components
 * 禁组件文件新增非组件导出（本策略函数须被单测直接 import）。
 */

/** 紧凑下拉菜单宽（w-48）与视口安全裕度：clamp 计算单源 */
export const COMPACT_SELECT_MENU_WIDTH = 192
export const COMPACT_SELECT_VIEWPORT_MARGIN = 12

/**
 * 紧凑下拉对齐策略（移动端视觉 P0-2，MuMu 实测 shots/05：思考强度浮窗
 * align=start 向右展开溢出视口右缘被裁）。视口 clamp：
 * ①右空间（视口右缘 − 裕度 − 触发器右缘）放得下菜单宽 → start（桌面常态）；
 * ②否则左空间放得下 → end（靠右触发器翻转向左展开）；
 * ③两侧都放不下 → 剩余空间大的一侧（radix collision 收进视口兜底）。
 */
export function compactSelectAlign(
  viewportWidth: number,
  triggerRight: number,
  menuWidth: number = COMPACT_SELECT_MENU_WIDTH,
  margin: number = COMPACT_SELECT_VIEWPORT_MARGIN,
): 'start' | 'end' {
  const spaceRight = viewportWidth - margin - triggerRight
  if (spaceRight >= menuWidth) return 'start'
  if (triggerRight - margin >= menuWidth) return 'end'
  return spaceRight >= triggerRight - margin ? 'start' : 'end'
}
