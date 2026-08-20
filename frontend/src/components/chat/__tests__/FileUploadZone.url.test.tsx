/** @feature FP-0.2.四 前端Schema @vision V6 可即用 @ci frontend-test */
/**
 * FileUploadZone 附件 url 修正测试（ADR 2026-08-21）。
 *
 * 修复前：上传成功后 onFilesChange 附件的 url 误填 file_id——
 * 消息 content 引用（appendAttachmentRefs）与前端渲染拿到的都是
 * 无效标识而非 /uploads/... 访问 URL。修复后 url 必须来自上传响应。
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FileUploadZone } from '../FileUploadZone'

const uploadFileMock = vi.fn()

vi.mock('@/services/api/files', () => ({
  uploadFile: (...args: unknown[]) => uploadFileMock(...args),
  validateFile: vi.fn().mockReturnValue({ valid: true }),
}))

function makeImageFile(name = 'cat.png') {
  return new File(['x'], name, { type: 'image/png' })
}

describe('FileUploadZone 附件 url 用上传响应的 /uploads URL', () => {
  beforeEach(() => {
    uploadFileMock.mockReset()
    ;(URL as unknown as Record<string, unknown>).createObjectURL = vi
      .fn()
      .mockReturnValue('blob:mock')
    ;(URL as unknown as Record<string, unknown>).revokeObjectURL = vi.fn()
  })
  afterEach(() => vi.clearAllMocks())

  it('上传成功后 onFilesChange 的附件 url 为 /uploads/...（非 file_id）', async () => {
    uploadFileMock.mockResolvedValue({
      file_id: 'f-abc123',
      filename: 'cat.png',
      mime_type: 'image/png',
      url: '/uploads/cat-abc123.png',
    })
    const onFilesChange = vi.fn()
    render(<FileUploadZone onFilesChange={onFilesChange} />)

    const input = document.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [makeImageFile()] } })

    await waitFor(() => expect(onFilesChange).toHaveBeenCalled())
    const attachments = onFilesChange.mock.lastCall[0]
    expect(attachments).toHaveLength(1)
    expect(attachments[0].url).toBe('/uploads/cat-abc123.png')
    expect(attachments[0].url).not.toBe('f-abc123')
    expect(attachments[0].id).toBe('f-abc123')
  })

  it('移除附件后重发的列表同样携带真实 url', async () => {
    uploadFileMock.mockResolvedValue({
      file_id: 'f-1',
      filename: 'a.png',
      mime_type: 'image/png',
      url: '/uploads/a-1.png',
    })
    const onFilesChange = vi.fn()
    const { container } = render(
      <FileUploadZone onFilesChange={onFilesChange} />,
    )

    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    fireEvent.change(input, { target: { files: [makeImageFile('a.png')] } })
    await waitFor(() =>
      expect(container.querySelectorAll('[aria-label^="移除"]')).toHaveLength(1),
    )

    // 点击移除按钮 → onFilesChange 以空列表再通知（列表中该附件已剔除）
    fireEvent.click(container.querySelectorAll('[aria-label^="移除"]')[0])
    await waitFor(() =>
      expect(onFilesChange).toHaveBeenLastCalledWith([]),
    )
  })
})
