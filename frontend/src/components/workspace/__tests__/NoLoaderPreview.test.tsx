/** @feature FP-T12 前端适配(工作区文件预览兜底卡) | @ci: frontend-test */
/**
 * NoLoaderPreview 行为锁——文件类型未命中加载器的工作区兜底卡：
 * 显式说明（文件名/扩展名/体积）+ 下载动作（成功取流落盘/失败 toast 带
 * HTTP 状态/无 URL 零动作）。mock 仅外部依赖（apiClient/sonner/URL）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders as render } from '@/test/renderWithProviders'

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }))

vi.mock('@/services/api/client', () => ({
  default: { get: vi.fn() },
}))

import { toast } from 'sonner'
import apiClient from '@/services/api/client'
import { NoLoaderPreview } from '../NoLoaderPreview'

describe('NoLoaderPreview — 无加载器兜底卡', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    URL.createObjectURL = vi.fn(() => 'blob:dl-1')
    URL.revokeObjectURL = vi.fn()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('渲染：文件名/扩展名/体积显式可见（路径分隔双形态）', () => {
    render(<NoLoaderPreview filePath="docs/spec.v2.ZIP" size={2048} />)
    expect(screen.getByText('spec.v2.ZIP')).toBeInTheDocument()
    expect(screen.getByText('(2.0 KB)')).toBeInTheDocument()
    expect(screen.getByText(/暂无支持 \.zip 类型的加载器/)).toBeInTheDocument()
    expect(screen.getByText('该文件类型暂无可用的在线查看器。')).toBeInTheDocument()
    // 反斜杠路径（Windows 形态）与无扩展名兜底
    const { unmount } = render(<NoLoaderPreview filePath="C:\\data\\README" />)
    expect(screen.getByText(/暂无支持 readme 类型的加载器/)).toBeInTheDocument()
    unmount()
  })

  it('下载成功：取流 → 建临时链接点击 → 释放 blob', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: new Blob(['x']) })
    render(<NoLoaderPreview filePath="a.bin" url="/api/files/a.bin" />)
    fireEvent.click(screen.getByRole('button', { name: /下载到本地查看/ }))
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:dl-1'))
    expect(apiClient.get).toHaveBeenCalledWith('/api/files/a.bin', expect.anything())
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('下载失败带 HTTP 状态：toast 报状态码', async () => {
    vi.mocked(apiClient.get).mockRejectedValue({ response: { status: 404 } })
    render(<NoLoaderPreview filePath="a.bin" url="/api/files/a.bin" />)
    fireEvent.click(screen.getByRole('button', { name: /下载到本地查看/ }))
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
    expect(vi.mocked(toast.error).mock.calls[0][0]).toContain('HTTP 404')
  })

  it('下载失败无状态（网络断）：toast 报网络语义', async () => {
    vi.mocked(apiClient.get).mockRejectedValue(new Error('network down'))
    render(<NoLoaderPreview filePath="a.bin" url="/api/files/a.bin" />)
    fireEvent.click(screen.getByRole('button', { name: /下载到本地查看/ }))
    await waitFor(() => expect(toast.error).toHaveBeenCalled())
    expect(vi.mocked(toast.error).mock.calls[0][0]).toContain('请检查网络')
  })

  it('无 url：点击零请求（按钮在但下载通道缺省不动作）', async () => {
    render(<NoLoaderPreview filePath="a.bin" />)
    fireEvent.click(screen.getByRole('button', { name: /下载到本地查看/ }))
    await waitFor(() => expect(screen.getByText(/暂无支持/)).toBeInTheDocument())
    expect(apiClient.get).not.toHaveBeenCalled()
  })
})
