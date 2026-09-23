/**
 * NotificationCenter 测试家族共享的数据工厂与 store 复位（navigate/panel 两文件同源）。
 * 曾在两个测试文件逐字复制（jscpd 克隆门禁重复源）。
 */
import { useInteractionStore, type PendingInteraction } from '@/stores/interactionStore'
import { useNotificationStore } from '@/stores/notificationStore'
import type { NotificationItem } from '@/types/notification'

export function makeNotification(overrides: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: 'n-1',
    category: 'info',
    title: '通知标题',
    priority: 'normal',
    isBlocking: false,
    isRead: false,
    timestamp: new Date().toISOString(),
    ...overrides,
  }
}

/** notification 模式的 pending 交互种子（字段取测试家族通用样例，按用例覆写） */
export function makePendingInteraction(
  overrides: Partial<PendingInteraction> = {},
): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'notification',
    title: '交互标题',
    description: '交互描述',
    threadId: 'th-1',
    tabId: 'tab-1',
    agentId: 'agent-1',
    timestamp: new Date().toISOString(),
    status: 'pending',
    ...overrides,
  }
}

/** 复位通知/交互两个真实 Zustand store（测试间隔离） */
export function resetNotificationStores(): void {
  useNotificationStore.setState({
    notifications: [],
    groupState: { collapsed: { critical: false, high: false, normal: true, low: true } },
    isPanelOpen: false,
    activeBlockingNotification: null,
  })
  useInteractionStore.setState({
    pendingInteractions: [],
    globalOpenRequestId: null,
    isMinimized: false,
  })
}
