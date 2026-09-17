// @feature FP-T12 前端组件补测
/**
 * 知识库页操作结果反馈行为测试
 *
 * 操作结果的成功/失败样式由结构化 ok 字段承载（文案只做展示）：
 * - 上传成功 → 成功样式
 * - 上传失败且错误文案不含"失败"字样 → 仍为失败样式（文案不能决定行为）
 */
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import apiClient from '@/services/api/client'
import { renderWithProviders } from '@/test/renderWithProviders'
import { KnowledgeBaseContent } from '../KnowledgeBaseContent'

vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: [] }),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

const mockPost = vi.mocked(apiClient.post)

function statusRegion(): HTMLElement {
  const region = screen.getByRole('status')
  expect(region.getAttribute('data-ok')).not.toBeNull()
  return region
}
/** 上传文件并等待操作状态区落定（data-ok 已写入），返回状态区 */
async function uploadAndAwaitStatus(user: typeof userEvent.setup, file = 'doc.txt'): Promise<HTMLElement> {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement
  await user.upload(input, new File(['content'], file))
  await waitFor(() => {
    expect(statusRegion().getAttribute('data-ok')).not.toBeNull()
  })
  return statusRegion()
}

beforeEach(() => {
  vi.clearAllMocks()
  apiClient.get.mockResolvedValue({ data: [] })
})

describe('KnowledgeBaseContent 操作结果反馈', () => {
  it('上传成功显示成功样式与成功文案', async () => {
    const user = userEvent.setup()
    mockPost.mockResolvedValueOnce({ data: {} })
    renderWithProviders(<KnowledgeBaseContent />)

    expect(await uploadAndAwaitStatus(user, 'doc.txt')).toHaveAttribute('data-ok', 'true')
    expect(statusRegion()).toHaveTextContent('文件 "doc.txt" 上传成功')
  })

  it('上传失败的错误文案不含"失败"字样时仍显示失败样式', async () => {
    const user = userEvent.setup()
    mockPost.mockRejectedValueOnce(new Error('磁盘空间不足'))
    renderWithProviders(<KnowledgeBaseContent />)

    expect(await uploadAndAwaitStatus(user, 'doc.txt')).toHaveAttribute('data-ok', 'false')
    expect(statusRegion()).toHaveTextContent('磁盘空间不足')
  })

  it('非 Error 抛出对象时显示失败样式的兜底文案', async () => {
    const user = userEvent.setup()
    mockPost.mockRejectedValueOnce('字符串异常')
    renderWithProviders(<KnowledgeBaseContent />)

    expect(await uploadAndAwaitStatus(user, 'doc.txt')).toHaveAttribute('data-ok', 'false')
    expect(statusRegion()).toHaveTextContent('上传失败')
  })
})
