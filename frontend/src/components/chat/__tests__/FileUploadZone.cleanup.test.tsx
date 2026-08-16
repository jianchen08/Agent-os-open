/**
 * FileUploadZone blob URL 生命周期测试
 *
 * 验证卸载清理闭包不再捕获首渲染的空 files：
 * 组件挂载后选中文件（生成 thumbnailUrl），卸载时必须 revokeObjectURL。
 */

import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FileUploadZone } from '../FileUploadZone'

vi.mock('@/services/api/files', () => ({
  uploadFile: vi.fn().mockResolvedValue({
    file_id: 'f-1',
    filename: 'a.png',
    mime_type: 'image/png',
    file_type: 'image',
    base64_data: '',
  }),
  validateFile: vi.fn().mockReturnValue({ valid: true }),
}))

describe('FileUploadZone 缩略图 blob URL 清理', () => {
  let createObjectURL: ReturnType<typeof vi.fn>
  let revokeObjectURL: ReturnType<typeof vi.fn>

  beforeEach(() => {
    createObjectURL = vi.fn().mockReturnValue('blob:mock-thumbnail')
    revokeObjectURL = vi.fn()
    // jsdom 未实现 createObjectURL，直接注入 spy
    ;(URL as unknown as Record<string, unknown>).createObjectURL = createObjectURL
    ;(URL as unknown as Record<string, unknown>).revokeObjectURL = revokeObjectURL
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('卸载后 revokeObjectURL 以最新 thumbnailUrl 被调用（而非首渲染空列表）', async () => {
    const { unmount } = render(<FileUploadZone />)

    const input = document.querySelector('input[type="file"]')
    expect(input).toBeTruthy()

    const file = new File(['x'], 'a.png', { type: 'image/png' })
    fireEvent.change(input!, { target: { files: [file] } })

    // 选中图片文件后应创建 blob URL
    await waitFor(() => {
      expect(createObjectURL).toHaveBeenCalledWith(file)
    })

    // 卸载：清理必须触达卸载前的最新 files
    // （修复前 cleanup 闭包捕获首渲染空数组，revokeObjectURL 不会被调用）
    unmount()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-thumbnail')
  })
})
