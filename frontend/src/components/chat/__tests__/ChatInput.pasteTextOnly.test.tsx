/**
 * ChatInput 粘贴降级路径测试（paste-to-path，纯文本模型）
 *
 * 移植语义（modlens）：模型不支持图片时，粘贴图片 → 上传 → 把可引用路径
 * 插入输入框（文本模型配合 read/识图工具使用）。附件管线（支持图片的模型）
 * 保持原行为。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ChatInput } from '../ChatInput'

vi.mock('@/hooks/useModelCapabilities', () => ({
  useModelCapabilities: () => ({
    inputCapabilities: {
      canDragDrop: true,
      canPasteImage: false,
      showAttachmentButton: false,
    },
    capabilities: { supportsAudio: false },
  }),
}))

vi.mock('@/services/api/files', () => ({
  uploadFile: vi.fn(async (file: File) => ({
    file_id: 'f1',
    filename: file.name,
    mime_type: 'image/png',
    media_type: 'image',
    size: 123,
    url: '/uploads/pasted-img.png',
  })),
  validateFile: vi.fn(() => ({ ok: true })),
}))

vi.mock('@/hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    isSupported: false,
    isRecording: false,
    state: 'idle',
    error: null,
    recordingDuration: 0,
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
  }),
}))

vi.mock('../VoiceInputButton', () => ({
  VoiceInputButton: () => <div data-testid="mock-voice-button" />,
}))

vi.mock('../ThinkingModeToggle', () => ({
  ThinkingModeToggle: () => <button type="button">thinking</button>,
}))

vi.mock('../ChatInputActions', () => ({
  ChatInputActions: () => <div data-testid="mock-chat-input-actions" />,
}))

function renderInput() {
  return render(
    <ChatInput
      mode="full"
      onSendMessage={() => {}}
      enableFileUpload
      enableDragDrop
      modelName="deepseek-v3"
      currentTokenUsage={0}
      maxTokens={10000}
      enableThinkingMode
    />,
  )
}

/** 构造粘贴事件：clipboardData 带一张 PNG 图片 */
function pasteImage() {
  const file = new File(['fake-png'], 'clipboard.png', { type: 'image/png' })
  const items = [
    {
      kind: 'file',
      type: 'image/png',
      getAsFile: () => file,
    },
  ]
  const clipboardData = { items, files: [file] }
  return fireEvent.paste(screen.getByRole('textbox'), { clipboardData })
}

describe('ChatInput 粘贴降级（text-only）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('模型不支持图片：粘贴图片 → 上传 → 路径文本插入输入框', async () => {
    const { uploadFile } = await import('@/services/api/files')
    renderInput()
    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement

    pasteImage()

    await waitFor(() => {
      expect(uploadFile).toHaveBeenCalledTimes(1)
      expect(textbox.value).toContain('图片已上传：/uploads/pasted-img.png')
    })
  })

  it('降级路径不产生附件（无 DataTransfer 附件管线调用）', async () => {
    const { uploadFile } = await import('@/services/api/files')
    renderInput()
    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement

    pasteImage()

    await waitFor(() => {
      expect(textbox.value).toContain('图片已上传')
    })
    // 附件预览区不应出现（canPasteImage=false 不建附件）
    expect(document.querySelector('[data-testid="attachment-preview"]')).toBeNull()
    expect(uploadFile).toHaveBeenCalledWith(expect.any(File), 'deepseek-v3')
  })

  it('上传失败：插入失败提示文本，不抛错', async () => {
    const files = await import('@/services/api/files')
    vi.mocked(files.uploadFile).mockRejectedValueOnce(new Error('network'))
    renderInput()
    const textbox = screen.getByRole('textbox') as HTMLTextAreaElement

    pasteImage()

    await waitFor(() => {
      expect(textbox.value).toContain('图片上传失败：clipboard.png')
    })
  })
})
