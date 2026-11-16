/**
 * 浏览器工具调用 → 工作区「浏览器」直播页签自动打开/激活
 *
 * tool_start 事件识别 browser_ 前缀工具名（browser_tool 六件：navigate/snapshot/
 * click/type/take_screenshot/console_messages）→ openDeclaredPluginPage 幂等打开
 * browser_tool.browser_live 声明页（已开=激活，未开=新开，并自动展开工作区）。
 * 用户裁定：使用即所见（ADR 2026-10-21-browser-live-workspace-tab）。
 * 声明软绑定：页面未装载/插件禁用时 openDeclaredPluginPage 显式报错通知，不静默。
 */

import { openDeclaredPluginPage } from '@/services/workspacePanelOpener'

const BROWSER_TOOL_PLUGIN_ID = 'browser_tool'
const BROWSER_LIVE_PAGE_ID = 'browser_live'

/** browser_tool 六件工具名前缀（tool_start 识别面；新工具随插件声明自动纳入） */
export const BROWSER_TOOL_NAME_PREFIX = 'browser_'

/** tool_start 事件里是浏览器工具时打开/激活直播页签（其余零动作） */
export function autoOpenBrowserLiveOnToolStart(toolName: string | undefined | null): boolean {
  if (!toolName || !toolName.startsWith(BROWSER_TOOL_NAME_PREFIX)) return false
  return openDeclaredPluginPage({ pluginId: BROWSER_TOOL_PLUGIN_ID, pageId: BROWSER_LIVE_PAGE_ID })
}
