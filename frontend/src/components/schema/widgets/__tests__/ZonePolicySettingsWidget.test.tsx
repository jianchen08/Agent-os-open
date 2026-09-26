/** @feature FP-0.2.四 前端Schema 授权区管理 widget | @ci frontend-test */
/**
 * 授权区管理 widget 测试（ADR 2026-09-24-read-deny-write-zones 批次3）
 *
 * 锁定契约：
 * 1. 挂载即 GET zones 回填两节名单（entries/read_deny）；
 * 2. 移除按钮 → POST zones/remove（section+path）→ 以响应 entries 更新该节；
 * 3. 追加 → POST zones/add（section+path）；
 * 4. GET 失败 → 错误态 + 重试入口（不静默空白）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ZonePolicySettingsWidget } from '../ZonePolicySettingsWidget'

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
  },
}))

const mockedToast = vi.fn()
vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: (...args: unknown[]) => mockedToast(...args) },
}))

function okData() {
  return {
    data: { entries: ['D:\\proj'], read_deny: ['C:\\secret'] },
  }
}

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  apiGet.mockResolvedValue(okData())
})

describe('ZonePolicySettingsWidget', () => {
  it('挂载即 GET zones 并回填两节名单', async () => {
    render(<ZonePolicySettingsWidget />)
    expect(apiGet).toHaveBeenCalledWith('/ext/pipeline_security_check/zones')
    await waitFor(() => {
      expect(screen.getByTitle('D:\\proj')).toBeInTheDocument()
      expect(screen.getByTitle('C:\\secret')).toBeInTheDocument()
    })
  })

  it('移除按钮 POST remove（section+path），以响应 entries 更新该节', async () => {
    apiPost.mockResolvedValue({ data: { removed: true, entries: [] } })
    render(<ZonePolicySettingsWidget />)
    await waitFor(() => screen.getByTitle('D:\\proj'))

    fireEvent.click(screen.getByLabelText('移除 D:\\proj'))

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/ext/pipeline_security_check/zones/remove', {
        section: 'entries',
        path: 'D:\\proj',
      })
    })
    await waitFor(() => {
      expect(screen.queryByTitle('D:\\proj')).not.toBeInTheDocument()
      // 另一节不受影响
      expect(screen.getByTitle('C:\\secret')).toBeInTheDocument()
    })
  })

  it('追加输入 + 按钮 POST add（section+path）', async () => {
    apiPost.mockResolvedValue({
      data: { added: true, entries: ['D:\\proj', 'E:\\new'] },
    })
    render(<ZonePolicySettingsWidget />)
    await waitFor(() => screen.getByTitle('D:\\proj'))

    const inputs = screen.getAllByPlaceholderText('目录绝对路径，如 D:\\myproject')
    fireEvent.change(inputs[0], { target: { value: 'E:\\new' } })
    fireEvent.click(screen.getAllByText('追加')[0])

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/ext/pipeline_security_check/zones/add', {
        section: 'entries',
        path: 'E:\\new',
      })
    })
    await waitFor(() => screen.getByTitle('E:\\new'))
  })

  it('GET 失败 → 错误态 + 重试入口', async () => {
    apiGet.mockRejectedValueOnce(new Error('boom'))
    render(<ZonePolicySettingsWidget />)
    await waitFor(() => screen.getByText(/授权名单加载失败/))
    expect(screen.getByText('重试')).toBeInTheDocument()

    // 重试成功 → 回到名单视图（reload 走同一 GET 通道）
    await waitFor(async () => undefined)
    fireEvent.click(screen.getByText('重试'))
    await waitFor(() => screen.getByTitle('D:\\proj'))
  })

  it('追加输入回车等价按钮：keydown Enter 触发 add', async () => {
    apiPost.mockResolvedValue({
      data: { added: true, entries: ['D:\\proj', 'C:\\secret', 'E:\\key'] },
    })
    render(<ZonePolicySettingsWidget />)
    await waitFor(() => screen.getByTitle('D:\\proj'))

    const inputs = screen.getAllByPlaceholderText('目录绝对路径，如 D:\\myproject')
    fireEvent.change(inputs[0], { target: { value: 'E:\\key' } })
    fireEvent.keyDown(inputs[0], { key: 'Enter' })

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/ext/pipeline_security_check/zones/add', {
        section: 'entries',
        path: 'E:\\key',
      })
    })
  })

  it('add 失败 → toast.error 提示且名单不变', async () => {
    apiPost.mockRejectedValueOnce(new Error('disk full'))
    render(<ZonePolicySettingsWidget />)
    await waitFor(() => screen.getByTitle('D:\\proj'))

    const inputs = screen.getAllByPlaceholderText('目录绝对路径，如 D:\\myproject')
    fireEvent.change(inputs[0], { target: { value: 'E:\\x' } })
    fireEvent.click(screen.getAllByText('追加')[0])

    await waitFor(() => {
      expect(mockedToast).toHaveBeenCalledWith(expect.stringContaining('追加失败'))
    })
    expect(screen.getByTitle('D:\\proj')).toBeInTheDocument()
  })

  it('deny 节移除走独立 section 回写', async () => {
    apiPost.mockResolvedValue({ data: { removed: true, entries: [] } })
    render(<ZonePolicySettingsWidget />)
    await waitFor(() => screen.getByTitle('C:\\secret'))

    fireEvent.click(screen.getByLabelText('移除 C:\\secret'))

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/ext/pipeline_security_check/zones/remove', {
        section: 'read_deny',
        path: 'C:\\secret',
      })
    })
  })
})
