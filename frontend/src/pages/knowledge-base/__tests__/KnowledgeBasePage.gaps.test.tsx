// @feature FP-T12 前端组件补测 | @ci frontend-test
/**
 * KnowledgeBasePage 覆盖缺口补充测试（与 actionFeedback 测试互补）：
 * - 拖拽三事件：dragover 高亮、dragleave 去高亮、drop 上传
 * - 删除条目：删除→确认/取消双态；确认调 DELETE + invalidate；失败文案
 *   （deleting 期间确认按钮禁用）
 * - 删除分类：X→✓/✕；确认删除；删除当前选中分类回落「全部」；失败文案
 * - 新建分类模态：空名禁用创建、Enter 提交、按钮提交成功关模态、失败文案
 * - 分类筛选：选中分类过滤条目；空分类显示专属空态
 * - 富数据渲染：统计三卡/条目分类与标签 chip/created_at/标签云
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import apiClient from '@/services/api/client'
import { renderWithProviders } from '@/test/renderWithProviders'
import { KnowledgeBasePage } from '../KnowledgeBasePage'
import { invalidateKnowledgeBaseCache, useKnowledgeBaseQuery } from '@/hooks/queries/useKnowledgeBaseQuery'

vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: [] }),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))
vi.mock('@/hooks/queries/useKnowledgeBaseQuery', () => ({
  useKnowledgeBaseQuery: vi.fn(),
  invalidateKnowledgeBaseCache: vi.fn(),
}))

const mockPost = vi.mocked(apiClient.post)
const mockDelete = vi.mocked(apiClient.delete)
const mockQuery = vi.mocked(useKnowledgeBaseQuery)
const mockInvalidate = vi.mocked(invalidateKnowledgeBaseCache)

const richData = {
  items: [
    {
      id: 'kb-1',
      name: '入门指南.md',
      size: 2048,
      categories: ['文档'],
      tags: ['入门', '教程'],
      created_at: '2026-09-01T00:00:00Z',
    },
    { id: 'kb-2', name: '接口笔记.txt', size: 512, categories: [] },
  ],
  stats: { total: 2, categories_count: 1, tags_count: 2 },
  categories: [
    { name: '文档', count: 1 },
    { name: '空分类' },
  ],
  tags: ['入门', '教程'],
}

function loaded(overrides: Record<string, unknown> = {}) {
  mockQuery.mockReturnValue({
    data: richData,
    isPending: false,
    isError: false,
    ...overrides,
  } as ReturnType<typeof useKnowledgeBaseQuery>)
  renderWithProviders(<KnowledgeBasePage />)
}

beforeEach(() => {
  vi.resetAllMocks()
  mockInvalidate.mockReturnValue(undefined)
})

describe('KnowledgeBasePage 拖拽上传', () => {
  it('dragover 高亮、dragleave 去高亮、drop 触发上传并计条数文案', async () => {
    loaded()
    mockPost.mockResolvedValue({ data: {} })
    const dropzone = screen.getByText(/点击上传/).closest('div[class*="border-dashed"]') as HTMLElement

    fireEvent.dragOver(dropzone)
    expect(dropzone.className).toContain('border-primary')
    expect(dropzone.className).toContain('bg-primary/5')

    fireEvent.dragLeave(dropzone)
    expect(dropzone.className).not.toContain('bg-primary/5')

    const file = new File(['x'], 'drag.txt')
    fireEvent.drop(dropzone, { dataTransfer: { files: [file] } })
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('文件 "drag.txt" 上传成功'),
    )
    expect(mockPost).toHaveBeenCalledTimes(1)
    expect(mockInvalidate).toHaveBeenCalled()
  })

  it('空文件列表 drop 不触发上传', () => {
    loaded()
    const dropzone = screen.getByText(/点击上传/).closest('div[class*="border-dashed"]') as HTMLElement
    fireEvent.drop(dropzone, { dataTransfer: { files: [] } })
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('上传进行中显示上传中提示（pending 期间）', () => {
    loaded()
    mockPost.mockReturnValue(new Promise(() => {})) // 永不 resolve：卡在 uploading
    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [new File(['x'], 'slow.txt')] } })
    expect(screen.getByText('上传中...')).toBeInTheDocument()
  })
})

describe('KnowledgeBasePage 删除条目', () => {
  it('确认删除：调 DELETE、显示成功文案并失效缓存；取消回到常态', async () => {
    loaded()
    mockDelete.mockResolvedValue({ data: {} })

    // 进入确认态
    fireEvent.click(screen.getAllByTitle('删除')[0])
    expect(screen.getByRole('button', { name: '确认' })).toBeInTheDocument()

    // 取消 → 回到删除按钮
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    expect(screen.queryByRole('button', { name: '确认' })).toBeNull()

    // 再确认 → 删除
    fireEvent.click(screen.getAllByTitle('删除')[0])
    fireEvent.click(screen.getByRole('button', { name: '确认' }))
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('"入门指南.md" 已删除'),
    )
    expect(mockDelete).toHaveBeenCalled()
    expect(mockInvalidate).toHaveBeenCalled()
  })

  it('删除失败显示错误消息；非 Error 抛出走兜底文案', async () => {
    loaded()
    mockDelete.mockRejectedValueOnce(new Error('被引用中'))
    fireEvent.click(screen.getAllByTitle('删除')[0])
    fireEvent.click(screen.getByRole('button', { name: '确认' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('被引用中'))

    mockDelete.mockRejectedValueOnce('字符串异常')
    fireEvent.click(screen.getAllByTitle('删除')[0])
    fireEvent.click(screen.getByRole('button', { name: '确认' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('删除失败'))
  })

  it('删除请求进行中确认按钮禁用', async () => {
    loaded()
    mockDelete.mockReturnValue(new Promise(() => {}))
    fireEvent.click(screen.getAllByTitle('删除')[0])
    fireEvent.click(screen.getByRole('button', { name: '确认' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: '确认' })).toBeDisabled(),
    )
  })
})

describe('KnowledgeBasePage 分类管理', () => {
  it('新建分类：模态开关、空名禁用、Enter 提交成功后清空关模态', async () => {
    loaded()
    mockPost.mockResolvedValue({ data: {} })

    fireEvent.click(screen.getByTitle('新建分类'))
    expect(screen.getByText('新建分类')).toBeInTheDocument()

    const nameInput = screen.getByPlaceholderText('输入分类名称')
    const createBtn = screen.getByRole('button', { name: '创建' }) as HTMLButtonElement
    expect(createBtn).toBeDisabled() // 空名禁用

    fireEvent.change(nameInput, { target: { value: '规范' } })
    expect(createBtn).toBeEnabled()
    fireEvent.keyDown(nameInput, { key: 'Enter' })

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('分类 "规范" 创建成功'),
    )
    expect(mockPost).toHaveBeenCalledWith(expect.anything(), { name: '规范' })
    expect(mockInvalidate).toHaveBeenCalled()
    expect(screen.queryByText('分类名称')).toBeNull() // 模态已关
  })

  it('新建分类：按钮提交失败显示兜底文案，模态保留', async () => {
    loaded()
    mockPost.mockRejectedValue('boom')
    fireEvent.click(screen.getByTitle('新建分类'))
    fireEvent.change(screen.getByPlaceholderText('输入分类名称'), { target: { value: 'X' } })
    fireEvent.click(screen.getByRole('button', { name: '创建' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('创建分类失败'))
    expect(screen.getByPlaceholderText('输入分类名称')).toBeInTheDocument()
  })

  it('删除分类：确认删除成功；删除当前选中分类回落「全部」', async () => {
    loaded()
    mockDelete.mockResolvedValue({ data: {} })

    // 先选中「文档」分类（条目过滤到 1 条；条目 chip 同名，取侧栏分类按钮）
    fireEvent.click(screen.getAllByText('文档')[0])
    expect(screen.getByText('入门指南.md')).toBeInTheDocument()
    expect(screen.queryByText('接口笔记.txt')).toBeNull()

    // 删除当前选中分类
    fireEvent.click(screen.getAllByTitle('删除分类')[0])
    fireEvent.click(screen.getByRole('button', { name: '✓' }))
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('分类 "文档" 已删除'),
    )
    // selectedCategory 复位 → 条目恢复全量
    expect(screen.getByText('接口笔记.txt')).toBeInTheDocument()
    expect(mockInvalidate).toHaveBeenCalled()
  })

  it('删除分类：✕ 取消确认态；删除失败显示兜底文案', async () => {
    loaded()
    fireEvent.click(screen.getAllByTitle('删除分类')[0])
    fireEvent.click(screen.getByRole('button', { name: '✕' }))
    expect(screen.queryByRole('button', { name: '✓' })).toBeNull()

    mockDelete.mockRejectedValue('down')
    fireEvent.click(screen.getAllByTitle('删除分类')[0])
    fireEvent.click(screen.getByRole('button', { name: '✓' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('删除分类失败'))
  })
})

describe('KnowledgeBasePage 数据呈现', () => {
  it('统计三卡、条目分类/标签 chip、created_at、标签云齐全', () => {
    loaded()
    expect(screen.getByText('总条目')).toBeInTheDocument()
    // 统计卡内取值（页面多处有裸数字，作用域内断言）
    const totalCard = screen.getByText('总条目').closest('div[class*="rounded-lg"]') as HTMLElement
    expect(totalCard).toHaveTextContent('2')
    // 标签云与条目标签 chip
    expect(screen.getAllByText('入门').length).toBeGreaterThanOrEqual(2)
    // created_at 时间戳（时区无关：仅断言行内存在时分秒形态）
    const row = screen.getByText('入门指南.md').closest('div[class*="rounded-lg"]') as HTMLElement
    expect(row.textContent).toMatch(/\d{1,2}:\d{2}:\d{2}/)
  })

  it('选中空分类 → 专属空态文案', () => {
    loaded()
    fireEvent.click(screen.getByText('空分类'))
    expect(screen.getByText('"空分类" 分类下暂无条目')).toBeInTheDocument()
  })

  it('查询失败：isError 显示错误状态（Error 实例取 message）', () => {
    loaded({ data: undefined, isPending: false, isError: true, error: new Error('net') })
    expect(screen.getByText('net')).toBeInTheDocument()
  })

  it('查询 pending 且无缓存 → 加载态', () => {
    mockQuery.mockReturnValue({
      data: undefined, isPending: true, isError: false,
    } as ReturnType<typeof useKnowledgeBaseQuery>)
    renderWithProviders(<KnowledgeBasePage />)
    expect(screen.getByText('共 0 条')).toBeInTheDocument()
  })
})
