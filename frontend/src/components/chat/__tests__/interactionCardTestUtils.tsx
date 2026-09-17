/**
 * InteractionCard 测试共享夹具：交互对象与卡片 props 工厂。
 *
 * vi.mock 声明因提升语义须留在各测试文件内字面量，此处只收可共享的纯工厂。
 */
import { vi } from 'vitest'
import type { PendingInteraction } from '@/stores/interactionStore'

export function makeInteraction(overrides: Partial<PendingInteraction> = {}): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'choice',
    title: '交互',
    description: '',
    threadId: 'th-1',
    tabId: 'tb-1',
    agentId: 'ag-1',
    timestamp: new Date().toISOString(),
    status: 'pending',
    ...overrides,
  }
}

export const cardProps = (interaction: PendingInteraction) => ({
  interaction,
  onRespondChoice: vi.fn(),
  onRespondText: vi.fn(),
  onNavigateToTab: vi.fn(),
  onDismiss: vi.fn(),
  isSubmitting: false,
})
