/**
 * useWorkspaceTabs — 工作区页签集合的组件侧读面（宿主布局态数据契约）
 *
 * [来源: docs/decisions/2026-10-10-widget-action-bus.md §5] widget 数据面不与
 * 具体 store 绑死：工作区页签集合是宿主布局状态（workspaceTabs 的消费语义
 * =「当前打开了哪些面板/页签」，如引导清单的 panel_visited 判定输入），
 * 经本 hook 暴露给 widget 层，widget 不感知 layoutModeStore 存储实现。
 *
 * hooks 订阅 layoutModeStore 的既有先例：useConnectionStatus /
 * useRealtimeEvents / useInteractionHandler。
 */

import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { WorkspaceTab } from '@/types/layout'

/** 当前工作区页签集合（开签/激活/关签响应式重渲染） */
export function useWorkspaceTabs(): WorkspaceTab[] {
  return useLayoutModeStore((s) => s.workspaceTabs)
}
