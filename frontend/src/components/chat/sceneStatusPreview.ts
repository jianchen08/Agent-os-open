/**
 * 现场预览文案（纯函数，从 TaskSceneStatusRow 拆出）
 *
 * 拆分理由：组件文件只导出组件（react-refresh/only-export-components）；
 * 文案规则要被单测直接断言，故独立成纯函数模块。
 */

/** 现场预览文案（一条摘要：活实例优先，其次产物，再次空态） */
export function buildScenePreviewText(
  liveCount: number,
  liveFocus: string | undefined,
  changeCount: number,
  latestChangePath: string | undefined,
): string {
  if (liveCount > 0) {
    const focus = liveFocus ? ` · ${liveFocus}` : ''
    return `${liveCount} 个实例运行中${focus}`
  }
  if (changeCount > 0) {
    const name = latestChangePath
      ? latestChangePath.replace(/\\/g, '/').split('/').pop()
      : undefined
    return name ? `产物 ${changeCount} 个文件 · ${name}` : `产物 ${changeCount} 个文件`
  }
  return '暂无现场活动'
}
