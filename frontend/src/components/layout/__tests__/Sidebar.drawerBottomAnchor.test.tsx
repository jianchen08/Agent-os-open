/** @feature FP-T12 前端适配 @ci: frontend-test
 * Sidebar 移动抽屉底部锚定（移动端视觉 P0-6，MuMu 实测 shots/03）：抽屉底栏
 * （用户/设置/主题/通知）贴屏幕底缘被裁。契约：
 * ①抽屉盒 = top/bottom 双锚定拉伸（inline height 不得再用 100dvh calc——
 *   与 top 偏移叠加的时序差是底行出屏根因，由 CSS 引擎直接求高）；
 * ②抽屉盒/底栏带安全区 padding（env 兜底）与常规底距，Android 无 notch
 *   （inset=0）时底栏也不贴死屏缘。
 */
import { screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { getSessions } from '@/services/api/session'
import { useUIStore } from '@/stores/uiStore'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import type { Session } from '@/types/models'

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal()
  return { ...actual, getSessions: vi.fn() }
})

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 'thread-1',
    title: '锚定契约会话',
    createdAt: '2026-10-12T00:00:00Z',
    updatedAt: '2026-10-12T00:00:00Z',
    messageCount: 0,
    status: 'active',
    metadata: {},
    agentId: null,
    workspace: null,
    isolationMode: null,
    pipelineIds: ['pipe-1'],
    pinned: false,
    starred: false,
    ...overrides,
  }
}

/** 抽屉根 aside 的移动展开态（fixed 抽屉）inline 高度声明 */
function drawerInlineHeight(): string {
  const aside = screen.getByTestId('sidebar')
  return (aside as HTMLElement).style.height
}

describe('Sidebar 移动抽屉底部锚定（P0-6）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useUIStore.setState({ sidebarCollapsed: false, mobileWorkspaceOpen: false })
  })

  it('移动展开态：抽屉盒高度不再走 100dvh calc（top/bottom 双锚定拉伸求高）', async () => {
    vi.mocked(getSessions).mockResolvedValue([makeSession()])
    renderWithProviders(<Sidebar isMobile />, { queryClient: createTestQueryClient() })
    await screen.findByRole('button', { name: /会话: 锚定契约会话/ })

    expect(drawerInlineHeight()).not.toContain('100dvh')
    expect(drawerInlineHeight()).toBe('auto')
  })

  it('抽屉盒与底栏都带安全区 padding（env 兜底 + 常规底距类）', async () => {
    vi.mocked(getSessions).mockResolvedValue([makeSession()])
    renderWithProviders(<Sidebar isMobile />, { queryClient: createTestQueryClient() })
    await screen.findByRole('button', { name: /会话: 锚定契约会话/ })

    const drawer = screen.getByTestId('sidebar')
    expect(drawer.className).toContain('safe-area-pb')
    const footer = screen.getByTestId('sidebar-footer')
    // 常规底距 + 安全区兜底合并的任意值类（无 notch/inset=0 时仍有 8px 底距）
    expect(footer.className).toContain('pb-[max(env(safe-area-inset-bottom,0px),var(--safe-inset-bottom,0px),8px)]')
  })
})
