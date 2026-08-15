/**
 * ChatPanelShell — ADR §五 ChatPanel 外壳入口
 *
 * 与 FiveSpaceLayout 对齐 Deep Space v2 App Shell，
 * 直接复用 FiveSpaceLayout，避免双份维护。
 *
 * 布局：TitleBar 32 · 单侧栏 288（折叠置顶）· ChatPanel · Workspace · StatusBar 22
 */

export { FiveSpaceLayout as ChatPanelShell } from './FiveSpaceLayout'
export type { FiveSpaceLayoutProps as ChatPanelShellProps } from './FiveSpaceLayout'
