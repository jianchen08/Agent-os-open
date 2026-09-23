// @feature FP-T12 前端组件补测
/**
 * 记忆页两分区聚合测试（memory + knowledge_base 页合并为「记忆」单页，用户裁定）
 *
 * - 默认展示「对话记忆」分区（原 MemoryPage 内容：统计卡 + 情景记忆列表）
 * - 「文档库」分区出现上传入口与条目列表加载占位（原 KnowledgeBasePage 内容）
 * - 分区切换各自状态保留：切回「对话记忆」内容原样可见
 *
 * 声明层（plugin.json 只剩 memory 页）另见
 * services/schema/__tests__/hindsightPanels.declared.test.ts
 */
import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryPage } from '@/pages/memory/MemoryPage'
import { renderWithProviders } from '@/test/renderWithProviders'
import { arrangeMemoryBaseResponses } from './memoryPageTestUtils'

vi.mock('@/services/api/memory', async () => (await import('./memoryPageTestUtils')).memoryApiModuleMock())

/** 文档库数据面 mock：isPending → 知识库内容渲染加载占位 */
vi.mock('@/hooks/queries/useKnowledgeBaseQuery', () => ({
  useKnowledgeBaseQuery: vi.fn(() => ({
    data: undefined,
    isPending: true,
    isError: false,
    error: null,
  })),
  invalidateKnowledgeBaseCache: vi.fn(),
}))


beforeEach(async () => {
  await arrangeMemoryBaseResponses()
})

describe('MemoryPage 两分区聚合', () => {
  it('默认展示对话记忆分区（统计卡可见，文档库未挂载）', async () => {
    renderWithProviders(<MemoryPage />)
    await screen.findByText('总记忆数')
    expect(screen.getByText('10')).toBeTruthy()
    // 文档库分区按需挂载：未切过去时上传入口不存在
    expect(screen.queryByText('点击上传')).toBeNull()
  })

  it('切「文档库」分区出现上传入口与条目列表加载占位', async () => {
    renderWithProviders(<MemoryPage />)
    await screen.findByText('总记忆数')
    fireEvent.click(screen.getByRole('tab', { name: '文档库' }))
    expect(await screen.findByText('点击上传')).toBeTruthy()
    expect(screen.getByText('加载中...')).toBeTruthy()
  })

  it('切回「对话记忆」分区内容保留', async () => {
    renderWithProviders(<MemoryPage />)
    await screen.findByText('总记忆数')
    fireEvent.click(screen.getByRole('tab', { name: '文档库' }))
    await screen.findByText('点击上传')
    fireEvent.click(screen.getByRole('tab', { name: '对话记忆' }))
    expect(screen.getByText('总记忆数')).toBeTruthy()
    expect(screen.getByText('10')).toBeTruthy()
  })
})
