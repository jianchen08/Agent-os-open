/** @feature FP-0.2.四 前端主题 | @ci: frontend-test */
/**
 * FileUploadZone 分类图标跟随主题状态色测试
 *
 * 图标色用语义状态 token（audio=pending/video=error），不再有调色板字面量；
 * 用两组有区分度的分类输入断言各自命中对应 token（防单值拟合）。
 */

import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FileUploadZone } from '../FileUploadZone'

vi.mock('@/services/api/files', () => ({
  uploadFile: vi.fn().mockResolvedValue({
    file_id: 'f-1',
    filename: 'a.mp3',
    mime_type: 'audio/mpeg',
  }),
  validateFile: vi.fn().mockReturnValue({ valid: true }),
}))

describe('FileUploadZone 分类图标语义色', () => {
  beforeEach(() => {
    const createObjectURL = vi.fn().mockReturnValue('blob:mock-thumbnail')
    const revokeObjectURL = vi.fn()
    ;(URL as unknown as Record<string, unknown>).createObjectURL = createObjectURL
    ;(URL as unknown as Record<string, unknown>).revokeObjectURL = revokeObjectURL
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  const addFiles = (files: File[]) => {
    const { container } = render(<FileUploadZone />)
    const input = document.querySelector('input[type="file"]')
    expect(input).toBeTruthy()
    fireEvent.change(input!, { target: { files } })
    return container
  }

  it('音频文件图标命中 text-status-pending（audio 分支）', async () => {
    const container = addFiles([new File(['x'], 'song.mp3', { type: 'audio/mpeg' })])
    await waitFor(() => {
      expect(container.querySelector('.text-status-pending')).toBeTruthy()
    })
  })

  it('视频文件图标命中 text-status-error（video 分支，与音频区分）', async () => {
    const container = addFiles([new File(['x'], 'clip.mp4', { type: 'video/mp4' })])
    await waitFor(() => {
      expect(container.querySelector('.text-status-error')).toBeTruthy()
    })
  })
})
