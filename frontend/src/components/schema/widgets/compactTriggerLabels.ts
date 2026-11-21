/**
 * 紧凑下拉触发器文案收口函数单源。
 *
 * 独立成文件的原因（2026-11-18 移动端视觉 P0 批同刀清偿）：FormWidget.tsx 是
 * 组件文件，react-refresh/only-export-components 禁组件文件导出非组件——
 * 本函数原随组件同文件导出，为 eslint error 棘轮（基线 689）清偿面。
 */

/**
 * 触发器窄屏收口文案（≤2 字）：去括注（「默认（命中规则才确认）」→「默认」）→
 * 去「模式」尾缀（「个人助理模式」→「个人助理」）→ 仍超 2 字取末 2 字
 * （「个人助手」→「助手」）。3 字且以否定前缀「免」开头保留首 2 字
 * （「免审批」→「免审」）——截成「审批」会把语义取反，比长标签更有害。
 * 全称不丢：菜单项与可访问名恒为原文；本函数只服务窄屏触发器
 * （桌面视口触发器走 triggerVisibleLabel 去括注，R10③）。
 */
export function compactTriggerLabel(label: string): string {
  const stripped = label
    .replace(/[（(][^）)]*[）)]/g, '')
    .replace(/模式$/, '')
    .trim()
  if (stripped.length <= 2) return stripped
  if (stripped.length === 3 && stripped.startsWith('免')) return stripped.slice(0, 2)
  return stripped.slice(-2)
}

/**
 * 触发器可见文案（R10③ 精简，用户原话"显示的描述太长了"）：去括注——
 * 「默认（命中规则才确认）」→「默认」、「免审批（隔离默认）」→「免审批」、
 * 「旁路（跳过审批）」→「旁路」。全称不丢：菜单项与可访问名恒为原文，
 * 本函数只服务触发器可见文案（2026-09-21 的"桌面视口显示原文"契约由本裁定
 * 收窄为"去括注"——括注说明活在菜单项 description/原文里，输入条只留裸档名）。
 */
export function triggerVisibleLabel(label: string): string {
  const stripped = label.replace(/[（(][^）)]*[）)]/g, '').trim()
  return stripped || label
}
