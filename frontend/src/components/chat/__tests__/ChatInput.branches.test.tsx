/** @feature FP-T12 前端适配 | @ci: frontend-test
 * ChatInput 分支补测：聚焦既有三个测试文件（layout/pasteTextOnly/sendReceipt）
 * 未覆盖的用户路径——文件选择与上传管线、拖拽上传、粘贴图片附件管线、
 * 语音输入（临时识别/转写完成/音频上传/录音状态条/停止提交）、展开编辑器
 * （Esc 收起）、外部文本插入桥、发送守卫（空输入/禁用态）、引用注入与消耗。
 *
 * 断言面：用户可见输出（渲染文本/占位符/aria 状态/DOM 副作用）与回调入参；
 * mock 仅限外部服务（文件上传 API/错误上报/模型能力/语音 hook 数据源），
 * UI 交互全部走真实 testing-library 事件。
 */
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { registerReferenceProvider } from '@/services/references/referenceProviders'
import { useChatInputStore } from '@/stores/chatInputStore'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import { ChatInput } from '../ChatInput'
import type { ChatInputProps, SendMessageParams } from '../types'
import type * as PipelinesApi from '@/services/api/pipelines'

/** 语音 hook 回调捕获类型（ChatInput 注入的行为出口） */
interface VoiceCallbacks {
  onInterimResult?: (text: string) => void
  onTranscriptionComplete?: (text: string) => void
  onRecordingComplete?: (blob: Blob) => void
  onError?: (error: { type: string; message: string }) => void
}

/** 外部服务替身（vi.hoisted：vi.mock 工厂与用例体共享同一实例） */
const mocks = vi.hoisted(() => ({
  getModels: vi.fn(),
  fetchPipelineStates: vi.fn(),
  uploadFile: vi.fn(),
  validateFile: vi.fn(),
  reportError: vi.fn(),
  caps: {
    canDragDrop: true,
    canPasteImage: true,
    showAttachmentButton: true,
    supportsAudio: false,
  },
  voice: {
    isSupported: false,
    isRecording: false,
    state: 'idle',
    mode: 'browser',
    recordingDuration: 0,
    error: null as { type: string; message: string } | null,
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
    captured: {} as VoiceCallbacks,
  },
}))

vi.mock('@/services/api/config', () => ({
  getModels: (...args: unknown[]) => mocks.getModels(...args),
}))
vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<typeof PipelinesApi>()),
  fetchPipelineStates: (...args: unknown[]) => mocks.fetchPipelineStates(...args),
}))
vi.mock('@/hooks/useModelCapabilities', () => ({
  useModelCapabilities: () => ({
    inputCapabilities: {
      canDragDrop: mocks.caps.canDragDrop,
      canPasteImage: mocks.caps.canPasteImage,
      showAttachmentButton: mocks.caps.showAttachmentButton,
    },
    capabilities: { supportsAudio: mocks.caps.supportsAudio },
  }),
}))
vi.mock('@/hooks/useVoiceInput', () => ({
  useVoiceInput: (options: VoiceCallbacks) => {
    mocks.voice.captured = options
    return {
      state: mocks.voice.state,
      isRecording: mocks.voice.isRecording,
      isTranscribing: mocks.voice.state === 'transcribing',
      transcript: '',
      recordingDuration: mocks.voice.recordingDuration,
      mode: mocks.voice.mode,
      error: mocks.voice.error,
      startRecording: mocks.voice.startRecording,
      stopRecording: mocks.voice.stopRecording,
      isSupported: mocks.voice.isSupported,
    }
  },
}))
vi.mock('../VoiceInputButton', () => ({
  VoiceInputButton: (props: {
    disabled?: boolean
    state?: string
    onClick?: () => void
    'aria-label'?: string
  }) => (
    <button
      type="button"
      data-testid="mock-voice-button"
      data-state={props.state ?? 'idle'}
      disabled={props.disabled}
      onClick={props.onClick}
    >
      {props['aria-label'] ?? '语音输入'}
    </button>
  ),
}))
vi.mock('../ChatInputActions', () => ({
  ChatInputActions: () => <div data-testid="mock-chat-input-actions" />,
}))
vi.mock('@/services/api/files', () => ({
  uploadFile: mocks.uploadFile,
  validateFile: mocks.validateFile,
}))
vi.mock('@/services/errorReporting', () => ({
  ErrorType: { NETWORK: 'network' },
  ErrorSeverity: { ERROR: 'error' },
  reportError: (...args: unknown[]) => mocks.reportError(...args),
}))

/** jsdom 无 DataTransfer：粘贴图片的附件管线需要构造 DataTransfer */
class StubDataTransfer {
  private collected: File[] = []
  readonly items = {
    add: (file: File): void => {
      this.collected.push(file)
    },
  }
  get files(): File[] {
    return this.collected
  }
}
vi.stubGlobal('DataTransfer', StubDataTransfer)
afterAll(() => {
  vi.unstubAllGlobals()
})

/** jsdom 无 URL.createObjectURL：图片预览需要桩（返回可追踪的假 blob URL） */
let blobSeq = 0

const textareaEl = () => screen.getByTestId('chat-input-textarea') as HTMLTextAreaElement
const fileInputEl = (container: HTMLElement) =>
  container.querySelector('input[type="file"]') as HTMLInputElement
const PLACEHOLDER_DEFAULT = 'Enter 发送 · Shift+Enter 换行 · 支持拖拽上传'
const PLACEHOLDER_DRAG = '松开鼠标上传文件'

/** 标准上传成功响应 */
const uploadOk = {
  file_id: 'f1',
  filename: 'notes.txt',
  mime_type: 'text/plain',
  media_type: 'document',
  size: 5,
  url: '/uploads/notes.txt',
}
const textFile = () => new File(['hello'], 'notes.txt', { type: 'text/plain' })

/** 渲染 ChatInput（可后续以增量 props 重渲染，驱动 disabled/录音态等变化） */
function renderInput(props: Partial<ChatInputProps> = {}) {
  const resolved: ChatInputProps = {
    mode: 'full',
    onSendMessage: () => {},
    enableFileUpload: true,
    enableDragDrop: true,
    modelName: 'deepseek-v3',
    ...props,
  }
  const rendered = render(<ChatInput {...resolved} />)
  const rerenderWith = (next: Partial<ChatInputProps>) =>
    rendered.rerender(<ChatInput {...resolved} {...next} />)
  return { ...rendered, rerenderWith }
}

beforeEach(() => {
  localStorage.clear()
  useChatInputStore.setState({ pendingInsert: null, drafts: {} })
  Object.assign(mocks.caps, {
    canDragDrop: true,
    canPasteImage: true,
    showAttachmentButton: true,
    supportsAudio: false,
  })
  Object.assign(mocks.voice, {
    isSupported: false,
    isRecording: false,
    state: 'idle',
    mode: 'browser',
    recordingDuration: 0,
    error: null,
  })
  mocks.uploadFile.mockReset()
  mocks.validateFile.mockReset()
  mocks.voice.startRecording.mockReset()
  mocks.voice.stopRecording.mockReset()
  mocks.reportError.mockReset()
  mocks.validateFile.mockImplementation(() => ({ valid: true }))
  // 模型注册表/states 保持挂起，避免测试结束后异步 resolve 触发 act() 告警噪声
  mocks.getModels.mockImplementation(() => new Promise(() => {}))
  mocks.fetchPipelineStates.mockImplementation(() => new Promise(() => {}))
  URL.createObjectURL = vi.fn(() => `blob:preview-${++blobSeq}`)
  URL.revokeObjectURL = vi.fn()
})

describe('ChatInput 文件选择与上传管线', () => {
  it('点击附件按钮 → 触发隐藏 file input 的系统选择', () => {
    const { container } = renderInput()
    const input = fileInputEl(container)
    expect(input).not.toBeNull()
    const clickSpy = vi.spyOn(input, 'click').mockImplementation(() => {})
    fireEvent.click(screen.getByRole('button', { name: '添加附件' }))
    expect(clickSpy).toHaveBeenCalledTimes(1)
  })

  it('选择文件 → 上传成功 → 附件预览出现（名称 + 大小），选择框 value 复位', async () => {
    mocks.uploadFile.mockResolvedValue(uploadOk)
    const { container } = renderInput()
    fireEvent.change(fileInputEl(container), { target: { files: [textFile()] } })
    // 事件消费后 value 复位：重复选择同名文件仍会触发 change
    expect(fileInputEl(container).value).toBe('')
    expect(await screen.findByText('notes.txt')).toBeInTheDocument()
    expect(screen.getByText('5 B')).toBeInTheDocument()
  })

  it('上传成功后发送：待传文件转换为附件随消息发出，发送受理后附件清空', async () => {
    mocks.uploadFile.mockResolvedValue(uploadOk)
    const onSendMessage = vi.fn()
    const { container } = renderInput({ onSendMessage })
    fireEvent.change(fileInputEl(container), { target: { files: [textFile()] } })
    await screen.findByText('notes.txt')
    fireEvent.click(screen.getByTestId('chat-send-button'))
    expect(onSendMessage).toHaveBeenCalledTimes(1)
    expect(onSendMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        content: '',
        attachments: [
          expect.objectContaining({
            name: 'notes.txt',
            type: 'text/plain',
            size: 5,
            url: '/uploads/notes.txt',
            status: 'completed',
          }),
        ],
      }),
    )
    await waitFor(() => expect(screen.queryByText('notes.txt')).toBeNull())
  })

  it('图片文件：生成预览 URL 渲染缩略图；移除时释放预览 URL', async () => {
    mocks.uploadFile.mockResolvedValue({
      ...uploadOk,
      filename: 'pic.png',
      mime_type: 'image/png',
      url: '/uploads/pic.png',
    })
    const imageFile = new File(['png'], 'pic.png', { type: 'image/png' })
    const { container } = renderInput()
    fireEvent.change(fileInputEl(container), { target: { files: [imageFile] } })
    await screen.findByText('pic.png')
    expect(URL.createObjectURL).toHaveBeenCalledWith(imageFile)
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    const previewUrl = vi.mocked(URL.createObjectURL).mock.results[0]?.value
    expect(typeof previewUrl).toBe('string')
    // 图片走 img 缩略图而非图标占位
    expect(screen.getByAltText('pic.png')).toHaveAttribute('src', previewUrl)
    // 移除 → 预览消失并释放预览 URL
    const revokeCountBefore = vi.mocked(URL.revokeObjectURL).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: '移除附件 pic.png' }))
    await waitFor(() => expect(screen.queryByText('pic.png')).toBeNull())
    expect(vi.mocked(URL.revokeObjectURL).mock.calls.length).toBeGreaterThan(revokeCountBefore)
  })

  it('校验失败：错误提示条出现且不上传；可关闭', async () => {
    mocks.validateFile.mockImplementation(() => ({ valid: false, error: '文件大小超过限制（最大 10MB）' }))
    const { container } = renderInput()
    fireEvent.change(fileInputEl(container), { target: { files: [textFile()] } })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('文件大小超过限制（最大 10MB）')
    expect(mocks.uploadFile).not.toHaveBeenCalled()
    expect(screen.queryByText('notes.txt')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '关闭错误提示' }))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('多文件混合：仅通过校验的进入上传，失败的单独立即报错', async () => {
    mocks.uploadFile.mockResolvedValue(uploadOk)
    mocks.validateFile.mockImplementation((file: File) =>
      file.type === 'text/plain'
        ? { valid: true }
        : { valid: false, error: '当前模型不支持图片输入' },
    )
    const { container } = renderInput()
    fireEvent.change(fileInputEl(container), {
      target: { files: [new File(['x'], 'bad.png', { type: 'image/png' }), textFile()] },
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('当前模型不支持图片输入')
    expect(await screen.findByText('notes.txt')).toBeInTheDocument()
    expect(screen.queryByText('bad.png')).toBeNull()
    expect(mocks.uploadFile).toHaveBeenCalledTimes(1)
  })

  it('上传失败：Error / 字符串 / 其他拒绝值都落到错误提示与上报', async () => {
    const { container } = renderInput()
    const input = fileInputEl(container)
    const cases: [unknown, string][] = [
      [new Error('网络中断'), '网络中断'],
      ['字符串错误', '字符串错误'],
      [undefined, '上传失败'],
    ]
    for (const [rejectWith, expected] of cases) {
      mocks.uploadFile.mockRejectedValueOnce(rejectWith)
      fireEvent.change(input, { target: { files: [textFile()] } })
      expect(await screen.findByRole('alert')).toHaveTextContent(expected)
      expect(mocks.reportError).toHaveBeenCalledWith(
        expected,
        expect.objectContaining({ operation: 'uploadFile', componentName: 'ChatInput' }),
      )
    }
  })

  it('上传中：附件进入 loading 态且移除按钮禁用；完成后恢复可移除', async () => {
    let resolveUpload: (value: typeof uploadOk) => void = () => {}
    mocks.uploadFile.mockImplementation(
      () => new Promise<typeof uploadOk>((res) => (resolveUpload = res)),
    )
    const { container } = renderInput()
    fireEvent.change(fileInputEl(container), { target: { files: [textFile()] } })
    expect(await screen.findByText('notes.txt')).toBeInTheDocument()
    const removeBtn = screen.getByRole('button', { name: '移除附件 notes.txt' })
    expect(removeBtn).toBeDisabled()
    resolveUpload(uploadOk)
    await waitFor(() => expect(removeBtn).not.toBeDisabled())
    fireEvent.click(removeBtn)
    await waitFor(() => expect(screen.queryByText('notes.txt')).toBeNull())
  })

  it('空文件列表（null / 空数组）不触发上传也不报错', () => {
    const { container } = renderInput()
    const input = fileInputEl(container)
    fireEvent.change(input, { target: { files: null } })
    fireEvent.change(input, { target: { files: [] } })
    expect(mocks.uploadFile).not.toHaveBeenCalled()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('ChatInput 拖拽上传', () => {
  it('dragOver 进入拖拽态（占位符提示 + 高亮环），dragLeave 恢复', () => {
    renderInput()
    const region = screen.getByTestId('chat-input')
    expect(textareaEl().placeholder).toBe(PLACEHOLDER_DEFAULT)
    fireEvent.dragOver(region)
    expect(textareaEl().placeholder).toBe(PLACEHOLDER_DRAG)
    expect(region.className).toContain('ring-2')
    fireEvent.dragLeave(region)
    expect(textareaEl().placeholder).toBe(PLACEHOLDER_DEFAULT)
  })

  it('drop 释放文件 → 进入上传管线并退出拖拽态', async () => {
    mocks.uploadFile.mockResolvedValue({
      ...uploadOk,
      filename: 'dropped.txt',
      url: '/uploads/dropped.txt',
    })
    renderInput()
    fireEvent.dragOver(screen.getByTestId('chat-input'))
    fireEvent.drop(screen.getByTestId('chat-input'), {
      dataTransfer: { files: [new File(['a'], 'dropped.txt', { type: 'text/plain' })] },
    })
    expect(await screen.findByText('dropped.txt')).toBeInTheDocument()
    expect(mocks.uploadFile).toHaveBeenCalledWith(expect.any(File), 'deepseek-v3')
    expect(textareaEl().placeholder).toBe(PLACEHOLDER_DEFAULT)
  })

  it('enableDragDrop=false：不进入拖拽态', () => {
    renderInput({ enableDragDrop: false })
    fireEvent.dragOver(screen.getByTestId('chat-input'))
    expect(textareaEl().placeholder).not.toBe(PLACEHOLDER_DRAG)
    expect(screen.getByTestId('chat-input').className).not.toContain('ring-2')
  })

  it('模型能力 canDragDrop=false：不进入拖拽态且 drop 不触发上传', () => {
    mocks.caps.canDragDrop = false
    renderInput()
    const region = screen.getByTestId('chat-input')
    fireEvent.dragOver(region)
    expect(textareaEl().placeholder).not.toBe(PLACEHOLDER_DRAG)
    fireEvent.drop(region, {
      dataTransfer: { files: [new File(['a'], 'x.txt', { type: 'text/plain' })] },
    })
    expect(mocks.uploadFile).not.toHaveBeenCalled()
  })

  it('enableFileUpload=false：上传开关在文件选择入口处拦下（不渲染附件按钮与选择框）', () => {
    renderInput({ enableFileUpload: false })
    expect(screen.queryByRole('button', { name: '添加附件' })).toBeNull()
    expect(document.querySelector('input[type="file"]')).toBeNull()
    // 拖拽仍可用（只受 enableDragDrop/canDragDrop 约束），但文件被上传开关拦下
    fireEvent.drop(screen.getByTestId('chat-input'), {
      dataTransfer: { files: [textFile()] },
    })
    expect(mocks.uploadFile).not.toHaveBeenCalled()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('ChatInput 粘贴图片', () => {
  function pasteOn(textarea: HTMLTextAreaElement, items: unknown[]) {
    const clipboardData = { items, files: [] }
    return fireEvent.paste(textarea, { clipboardData })
  }
  type PasteItem = { kind: string; type: string; getAsFile: () => File | null }

  it('canPasteImage=true：粘贴图片走附件上传管线，不插 markdown 文本', async () => {
    mocks.uploadFile.mockResolvedValue({
      ...uploadOk,
      filename: 'clipboard.png',
      mime_type: 'image/png',
      media_type: 'image',
      url: '/uploads/clipboard.png',
    })
    renderInput()
    const file = new File(['p'], 'clipboard.png', { type: 'image/png' })
    pasteOn(textareaEl(), [{ kind: 'file', type: 'image/png', getAsFile: () => file }])
    expect(await screen.findByText('clipboard.png')).toBeInTheDocument()
    expect(textareaEl().value).toBe('')
    expect(mocks.uploadFile).toHaveBeenCalledWith(expect.any(File), 'deepseek-v3')
  })

  it.each<[string, PasteItem[]]>([
    ['非图片 item', [{ kind: 'string', type: 'text/plain', getAsFile: () => null }]],
    ['图片 item 但取不到文件', [{ kind: 'file', type: 'image/png', getAsFile: () => null }]],
  ])('%s：不拦截粘贴、不产生附件', (_name, items) => {
    renderInput()
    pasteOn(textareaEl(), items)
    expect(mocks.uploadFile).not.toHaveBeenCalled()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText('clipboard.png')).toBeNull()
  })

  it('纯文本模型（canPasteImage=false）+ draftKey：插入 markdown 引用并同步草稿', async () => {
    mocks.caps.canPasteImage = false
    mocks.uploadFile.mockResolvedValue({
      ...uploadOk,
      filename: 'my shot.png',
      mime_type: 'image/png',
      media_type: 'image',
      url: '/uploads/pasted.png',
    })
    renderInput({ draftKey: 'paste-draft' })
    pasteOn(textareaEl(), [
      {
        kind: 'file',
        type: 'image/png',
        getAsFile: () => new File(['p'], 'my shot.png', { type: 'image/png' }),
      },
    ])
    await waitFor(() => {
      expect(textareaEl().value).toContain('![my shot.png](/uploads/pasted.png)')
      expect(useChatInputStore.getState().drafts['paste-draft']).toContain(
        '![my shot.png](/uploads/pasted.png)',
      )
    })
  })
})

describe('ChatInput 语音输入（实时识别模式）', () => {
  it('空闲点击 → startRecording；录音中点击 → stopRecording（aria 状态同步）', () => {
    mocks.voice.isSupported = true
    const { rerenderWith } = renderInput()
    const btn = () => screen.getByTestId('mock-voice-button')
    expect(btn()).toHaveTextContent('开始语音输入')
    expect(btn()).not.toBeDisabled()
    fireEvent.click(btn())
    expect(mocks.voice.startRecording).toHaveBeenCalledTimes(1)
    expect(mocks.voice.stopRecording).not.toHaveBeenCalled()
    // 录音态由 hook 状态驱动 → props 变化重渲染
    mocks.voice.isRecording = true
    mocks.voice.state = 'recording'
    rerenderWith({})
    expect(btn()).toHaveTextContent('停止语音输入')
    fireEvent.click(btn())
    expect(mocks.voice.stopRecording).toHaveBeenCalledTimes(1)
    expect(mocks.voice.startRecording).toHaveBeenCalledTimes(1)
  })

  it.each<[string, number, string, string]>([
    ['browser', 0, '正在聆听…', '00:00'],
    ['browser', 65, '正在聆听…', '01:05'],
    ['server-asr', 5, '服务器识别中…', '00:05'],
  ])('录音状态条：mode=%s 时长=%s 显示 %s 与 %s', (mode, duration, label, durationText) => {
    Object.assign(mocks.voice, {
      isSupported: true,
      isRecording: true,
      state: 'recording',
      mode,
      recordingDuration: duration,
    })
    renderInput()
    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.getByText(durationText)).toBeInTheDocument()
  })

  it('临时识别：空 interim 不改动输入', () => {
    mocks.voice.isSupported = true
    renderInput()
    act(() => mocks.voice.captured.onInterimResult?.(''))
    expect(textareaEl().value).toBe('')
  })

  it('临时识别：首次直接追加；已有正文时先补空格', () => {
    mocks.voice.isSupported = true
    renderInput()
    act(() => mocks.voice.captured.onInterimResult?.('你好'))
    expect(textareaEl().value).toBe('你好')
    // 键盘编辑使临时区间失效 → 新一轮临时识别重新追加
    fireEvent.change(textareaEl(), { target: { value: 'abc' } })
    act(() => mocks.voice.captured.onInterimResult?.('你好'))
    expect(textareaEl().value).toBe('abc 你好')
  })

  it('临时识别：后续 interim 就地替换上一段临时文字', () => {
    mocks.voice.isSupported = true
    renderInput()
    act(() => mocks.voice.captured.onInterimResult?.('你好'))
    act(() => mocks.voice.captured.onInterimResult?.('你好世界'))
    expect(textareaEl().value).toBe('你好世界')
  })

  it('转写完成：剔除未确认临时段，追加确认文字（不重复）', () => {
    mocks.voice.isSupported = true
    renderInput()
    act(() => mocks.voice.captured.onInterimResult?.('临时'))
    act(() => mocks.voice.captured.onTranscriptionComplete?.('确认文字'))
    expect(textareaEl().value).toBe('确认文字')
  })

  it.each<[string, string, string, string, string]>([
    ['已有正文无临时段时补空格拼接', 'abc', '', '确认', 'abc 确认'],
    ['纯空白 final 视为无确认文字，输入保持不变', '', '临时', '   ', '临时'],
  ])('转写完成：%s', (_name, seed, interim, final, expected) => {
    mocks.voice.isSupported = true
    renderInput()
    if (seed) fireEvent.change(textareaEl(), { target: { value: seed } })
    if (interim) act(() => mocks.voice.captured.onInterimResult?.(interim))
    act(() => mocks.voice.captured.onTranscriptionComplete?.(final))
    expect(textareaEl().value).toBe(expected)
  })

  it('转写完成 + draftKey：确认文字同步草稿', () => {
    mocks.voice.isSupported = true
    renderInput({ draftKey: 'voice-draft' })
    act(() => mocks.voice.captured.onTranscriptionComplete?.('确认文字'))
    expect(useChatInputStore.getState().drafts['voice-draft']).toBe('确认文字')
  })

  it('语音识别错误 → 错误提示条展示', () => {
    mocks.voice.isSupported = true
    renderInput()
    act(() =>
      mocks.voice.captured.onError?.({ type: 'permission_denied', message: '麦克风权限被拒绝' }),
    )
    expect(screen.getByRole('alert')).toHaveTextContent('麦克风权限被拒绝')
  })

  it('停止录音：未确认临时文字提交为正文并保存草稿', () => {
    mocks.voice.isSupported = true
    const { rerenderWith } = renderInput({ draftKey: 'stop-draft' })
    const btn = () => screen.getByTestId('mock-voice-button')
    fireEvent.click(btn())
    act(() => mocks.voice.captured.onInterimResult?.('临时词'))
    mocks.voice.isRecording = true
    mocks.voice.state = 'recording'
    rerenderWith({})
    fireEvent.click(btn())
    expect(mocks.voice.stopRecording).toHaveBeenCalledTimes(1)
    expect(textareaEl().value).toBe('临时词')
    expect(useChatInputStore.getState().drafts['stop-draft']).toBe('临时词')
  })

  it('无临时文字时停止：只停录，不写草稿', () => {
    mocks.voice.isSupported = true
    const { rerenderWith } = renderInput({ draftKey: 'stop-clean' })
    const btn = () => screen.getByTestId('mock-voice-button')
    fireEvent.click(btn())
    mocks.voice.isRecording = true
    mocks.voice.state = 'recording'
    rerenderWith({})
    fireEvent.click(btn())
    expect(mocks.voice.stopRecording).toHaveBeenCalledTimes(1)
    expect(useChatInputStore.getState().drafts['stop-clean']).toBeUndefined()
  })
})

describe('ChatInput 语音录音完成（音频模型 supportsAudio）', () => {
  beforeEach(() => {
    mocks.caps.supportsAudio = true
    mocks.voice.isSupported = true
  })

  it('录音完成 → webm 音频文件进入上传管线并成为附件预览', async () => {
    mocks.uploadFile.mockResolvedValue({
      ...uploadOk,
      filename: 'voice.webm',
      mime_type: 'audio/webm',
      media_type: 'audio',
      url: '/uploads/voice.webm',
    })
    renderInput()
    await act(async () => {
      mocks.voice.captured.onRecordingComplete?.(new Blob(['audio'], { type: 'audio/webm' }))
    })
    expect(mocks.validateFile).toHaveBeenCalledWith(
      expect.any(File),
      expect.objectContaining({ supportsAudio: true }),
    )
    expect(mocks.uploadFile).toHaveBeenCalledWith(expect.any(File), 'deepseek-v3')
    const uploaded = mocks.uploadFile.mock.calls[0][0] as File
    expect(uploaded.name).toMatch(/^voice_\d+\.webm$/)
    expect(uploaded.type).toBe('audio/webm')
    expect(await screen.findByText(/^voice_\d+\.webm$/)).toBeInTheDocument()
  })

  it('音频校验失败：提示错误且不进入上传', async () => {
    mocks.validateFile.mockImplementation(() => ({ valid: false, error: '当前模型不支持音频输入' }))
    renderInput()
    await act(async () => {
      mocks.voice.captured.onRecordingComplete?.(new Blob(['a'], { type: 'audio/webm' }))
    })
    expect(screen.getByRole('alert')).toHaveTextContent('当前模型不支持音频输入')
    expect(mocks.uploadFile).not.toHaveBeenCalled()
  })

  it.each<[string, unknown, string]>([
    ['Error 拒绝取其 message', new Error('asr 网关超时'), 'asr 网关超时'],
    ['非 Error 拒绝用默认文案', undefined, '音频上传失败'],
  ])('音频上传失败：%s', async (_name, rejectWith, expected) => {
    mocks.uploadFile.mockRejectedValueOnce(rejectWith)
    renderInput()
    await act(async () => {
      mocks.voice.captured.onRecordingComplete?.(new Blob(['a'], { type: 'audio/webm' }))
    })
    expect(screen.getByRole('alert')).toHaveTextContent(expected)
    expect(mocks.reportError).toHaveBeenCalledWith(
      expected,
      expect.objectContaining({ operation: 'uploadVoiceFile', componentName: 'ChatInput' }),
    )
  })
})

describe('ChatInput 展开编辑器', () => {
  it('点击展开 → 高度锁定 80% 视口，按钮切为收起态', () => {
    renderInput()
    const toggle = screen.getByTestId('chat-input-expand-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveAttribute('aria-label', '收起编辑器')
    expect(textareaEl().style.height).toBe(`${Math.round(window.innerHeight * 0.8)}px`)
  })

  it('展开态 Esc → 收起；非展开态 Esc → 保持收起', () => {
    renderInput()
    const toggle = screen.getByTestId('chat-input-expand-toggle')
    fireEvent.click(toggle)
    fireEvent.keyDown(textareaEl(), { key: 'Escape' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveAttribute('aria-label', '展开编辑器')
    // 非展开态再按 Esc：状态不变
    fireEvent.keyDown(textareaEl(), { key: 'Escape' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('收起按钮再次点击可切回展开态', () => {
    renderInput()
    const toggle = screen.getByTestId('chat-input-expand-toggle')
    fireEvent.click(toggle)
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveAttribute('aria-label', '展开编辑器')
  })
})

describe('ChatInput 发送守卫', () => {
  it('空输入按 Enter：不发送', () => {
    const onSendMessage = vi.fn()
    renderInput({ onSendMessage })
    fireEvent.keyDown(textareaEl(), { key: 'Enter' })
    expect(onSendMessage).not.toHaveBeenCalled()
  })

  it('禁用态：输入与发送按钮均禁用；有文本按 Enter 不发送不清空', () => {
    const onSendMessage = vi.fn()
    const { rerenderWith } = renderInput({ onSendMessage })
    fireEvent.change(textareaEl(), { target: { value: '被禁用的输入' } })
    rerenderWith({ disabled: true })
    expect(textareaEl()).toBeDisabled()
    expect(screen.getByTestId('chat-send-button')).toBeDisabled()
    fireEvent.keyDown(textareaEl(), { key: 'Enter' })
    expect(onSendMessage).not.toHaveBeenCalled()
    expect(textareaEl().value).toBe('被禁用的输入')
  })
})

describe('ChatInput 引用注入与消耗（referenceProviders 注册缝）', () => {
  const REF_SOURCE = 'ut-ref'
  beforeEach(() => {
    // 中立化上一用例残留（同 source 覆盖注册）
    registerReferenceProvider({ source: REF_SOURCE, getSelection: () => null })
  })

  it('发送时注入待发引用块，受理后消耗 provider', () => {
    const consume = vi.fn()
    registerReferenceProvider({
      source: REF_SOURCE,
      getSelection: () => ({
        source: REF_SOURCE,
        attrs: { scene: 'main.tscn' },
        items: [{ name: 'Enemy', type: 'node', path: '/root/Enemy', extra: 'position=(1,2,3)' }],
      }),
      consume,
    })
    const onSendMessage = vi.fn()
    renderInput({ onSendMessage })
    fireEvent.change(textareaEl(), { target: { value: '看看这个' } })
    fireEvent.click(screen.getByTestId('chat-send-button'))
    const params = vi.mocked(onSendMessage).mock.calls[0][0] as SendMessageParams
    expect(params.content.startsWith('看看这个\n\n<reference')).toBe(true)
    expect(params.content).toContain('<reference source="ut-ref" scene="main.tscn">')
    expect(params.content).toContain('- Enemy (node) @ /root/Enemy [position=(1,2,3)]')
    expect(params.content.endsWith('</reference>')).toBe(true)
    expect(consume).toHaveBeenCalledTimes(1)
    expect(textareaEl().value).toBe('')
  })

  it('无待发引用（null selection）→ 正文不注入，consume 仍被枚举调用', () => {
    const consume = vi.fn()
    registerReferenceProvider({ source: REF_SOURCE, getSelection: () => null, consume })
    const onSendMessage = vi.fn()
    renderInput({ onSendMessage })
    fireEvent.change(textareaEl(), { target: { value: '纯文本' } })
    fireEvent.click(screen.getByTestId('chat-send-button'))
    expect(onSendMessage).toHaveBeenCalledWith(expect.objectContaining({ content: '纯文本' }))
    expect(consume).toHaveBeenCalledTimes(1)
  })

  it('发送未受理（false）→ provider 不消耗，输入保留供重试', () => {
    const consume = vi.fn()
    registerReferenceProvider({
      source: REF_SOURCE,
      getSelection: () => ({ source: REF_SOURCE, items: [{ name: 'A', type: 't', path: '/a' }] }),
      consume,
    })
    const onSendMessage = vi.fn(() => false)
    renderInput({ onSendMessage })
    fireEvent.change(textareaEl(), { target: { value: '未受理的消息' } })
    fireEvent.click(screen.getByTestId('chat-send-button'))
    expect(consume).not.toHaveBeenCalled()
    expect(textareaEl().value).toBe('未受理的消息')
  })
})

describe('ChatInput 外部插入文本（chatInputStore 桥）', () => {
  it('挂载即消费 store 中已有的待插入文本，并清空待插入 + 同步草稿', () => {
    act(() => {
      useChatInputStore.getState().requestInsert('外部引用文本')
    })
    renderInput({ draftKey: 'insert-mount' })
    expect(textareaEl().value).toBe('外部引用文本')
    expect(useChatInputStore.getState().pendingInsert).toBeNull()
    expect(useChatInputStore.getState().drafts['insert-mount']).toBe('外部引用文本')
  })

  it('挂载后外部请求插入：换行追加到已有正文，并清空待插入', () => {
    renderInput({ draftKey: 'insert-live' })
    fireEvent.change(textareaEl(), { target: { value: '已有' } })
    act(() => {
      useChatInputStore.getState().requestInsert('插入块')
    })
    expect(textareaEl().value).toBe('已有\n插入块')
    expect(useChatInputStore.getState().pendingInsert).toBeNull()
  })
})
