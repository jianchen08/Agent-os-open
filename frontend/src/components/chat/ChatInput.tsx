/** 统一的聊天输入组件 支持三种模式： */

import {
  AlertCircle,
  Database,
  File as FileIcon,
  Image as ImageIcon,
  Loader2,
  Maximize2,
  Minimize2,
  Paperclip,
  Send,
  Square,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { Button } from '@/components/ui/button'
import { useModelCapabilities } from '@/hooks/useModelCapabilities'
import { useVoiceInput } from '@/hooks/useVoiceInput'
import { cn } from '@/lib/utils'
import { uploadFile, validateFile } from '@/services/api/files'
import { ErrorSeverity, ErrorType, reportError } from '@/services/errorReporting'
import { useChatInputStore } from '@/stores/chatInputStore'
import { ThinkingModeToggle } from './ThinkingModeToggle'
import { VoiceInputButton } from './VoiceInputButton'
import type { Attachment, ChatInputProps, PendingFile, SendMessageParams } from './types'
import type { ThinkingModeState } from '@/types/thinkingMode'

/** 格式化文件大小 */
const formatFileSize = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** 格式化数字（添加千位分隔符） */
const formatNumber = (num: number): string => {
  return num.toLocaleString('en-US')
}

/** 格式化录音时长为 mm:ss */
const formatDuration = (seconds: number): string => {
  const m = Math.floor(seconds / 60)
    .toString()
    .padStart(2, '0')
  const s = (seconds % 60).toString().padStart(2, '0')
  return `${m}:${s}`
}

/** 附件预览组件 */
const AttachmentPreview = ({
  attachment,
  onRemove,
}: {
  attachment: Attachment | PendingFile
  onRemove: () => void
}) => {
  const isPendingFile = 'file' in attachment
  const isImage = isPendingFile
    ? attachment.file.type.startsWith('image/')
    : attachment.type?.startsWith('image/')
  const status = isPendingFile ? attachment.status : (attachment as Attachment).status
  const fileName = isPendingFile ? attachment.file.name : attachment.name
  const fileSize = isPendingFile ? attachment.file.size : attachment.size
  const previewUrl = isPendingFile ? attachment.previewUrl : attachment.previewUrl

  return (
    <div
      className={cn(
        'group relative flex items-center gap-2 rounded-xl p-2 transition-all duration-200',
        status === 'error'
          ? 'bg-destructive/10 border-destructive/50 border'
          : 'bg-muted/50 border-border/30 hover:border-border/50 border hover:shadow-sm',
      )}
    >
      {/* 预览图标/缩略图 */}
      {previewUrl ? (
        <img src={previewUrl} alt={fileName} className="h-10 w-10 rounded-lg object-cover" />
      ) : (
        <div className="bg-background/80 flex h-10 w-10 items-center justify-center rounded-lg">
          {isImage ? (
            <ImageIcon className="text-muted-foreground h-5 w-5" />
          ) : (
            <FileIcon className="text-muted-foreground h-5 w-5" />
          )}
        </div>
      )}

      {/* 文件信息 */}
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{fileName}</div>
        <div className="text-muted-foreground text-xs">{formatFileSize(fileSize)}</div>
      </div>

      {/* 上传状态 */}
      {status === 'uploading' && <Loader2 className="text-primary h-4 w-4 animate-spin" />}
      {status === 'error' && <AlertCircle className="text-destructive h-4 w-4" />}

      {/* 删除按钮 */}
      <Button
        variant="ghost"
        size="sm"
        className="hover:bg-destructive/10 hover:text-destructive h-6 w-6 rounded-lg p-0 opacity-100 md:opacity-0 md:group-hover:opacity-100"
        onClick={onRemove}
        disabled={status === 'uploading'}
        aria-label={`移除附件 ${fileName}`}
      >
        <X className="h-4 w-4" />
      </Button>
    </div>
  )
}

/** 统一的聊天输入组件 */
export const ChatInput = ({
  mode = 'full',
  disabled = false,
  isGenerating = false,
  executionState = 'idle',
  placeholder: _placeholder,
  onSendMessage,
  onStopGenerate,
  enableFileUpload = true,
  enableDragDrop = true,
  modelName,
  currentTokenUsage = 0,
  maxTokens = 0,
  completionTokens: _completionTokens = 0,
  totalTokens: _totalTokens = 0,
  enableThinkingMode = false,
  thinkingMode,
  toggleThinkingMode,
  className = '',
  draftKey,
}: ChatInputProps) => {
  /** 获取模型能力配置 */
  const { inputCapabilities } = useModelCapabilities(modelName)

  /** 初始化时从草稿 store 加载文本 */
  const [text, setText] = useState(() => {
    if (draftKey) {
      return useChatInputStore.getState().loadDraft(draftKey)
    }
    return ''
  })
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([])
  const [isDragging, setIsDragging] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [isExpanded, setIsExpanded] = useState(false)

  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  /** 追踪最新文本值，用于组件卸载时保存草稿 */
  const textRef = useRef(text)
  /** 语音实时识别：临时文字在 text 中的起始偏移量，-1 表示无未确认临时文字 */
  const interimVoiceStartRef = useRef(-1)

  /** 思考模式状态：优先使用外部传入的值 */
  const currentThinkingMode: ThinkingModeState = thinkingMode || {
    enabled: false,
    currentModel: modelName || '',
    switching: false,
  }
  const currentToggleThinkingMode =
    toggleThinkingMode ||
    (async (_enabled: boolean) => {
      // 思考模式切换由外部控制
    })

  /** 必须声明在使用它的回调（handleVoiceInterim / handleVoiceTranscriptionComplete 等）之前，
   *  否则在依赖数组中访问会触发 TDZ（Cannot access ... before initialization）。
   *  非展开态：随内容自适应，上限为视口高度的 1/3；展开态：固定高度，不随内容变化。*/
  const adjustTextareaHeight = useCallback(() => {
    if (isExpanded) return
    const textarea = textareaRef.current
    if (textarea) {
      const maxHeight = typeof window !== 'undefined' ? window.innerHeight / 3 : 200
      textarea.style.height = 'auto'
      textarea.style.height = `${Math.min(textarea.scrollHeight, maxHeight)}px`
    }
  }, [isExpanded])

  /** 展开：高度固定为聊天区域约 80%；收起：恢复 1/3 视口自适应 */
  useEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    if (isExpanded) {
      const vh = typeof window !== 'undefined' ? window.innerHeight : 600
      textarea.style.height = `${Math.round(vh * 0.8)}px`
    } else {
      adjustTextareaHeight()
    }
  }, [isExpanded, adjustTextareaHeight])

  /** 判断是否正在执行/生成 */
  const isExecuting = mode === 'smart' ? executionState === 'running' : isGenerating

  /** 获取模型是否支持音频 */
  const { capabilities } = useModelCapabilities(modelName)
  const supportsAudio = capabilities?.supportsAudio ?? false

  /** 处理语音录音完成（模型支持音频时） */
  const handleVoiceRecordingComplete = useCallback(
    async (audioBlob: Blob) => {
      const timestamp = Date.now()
      const audioFile = new File([audioBlob], `voice_${timestamp}.webm`, {
        type: audioBlob.type || 'audio/webm',
      })

      const validation = validateFile(audioFile, capabilities)
      if (!validation.valid) {
        setUploadError(validation.error || '音频文件验证失败')
        return
      }

      const pendingFile: PendingFile = {
        id: `voice-${timestamp}`,
        file: audioFile,
        status: 'pending',
      }

      setPendingFiles((prev) => [...prev, pendingFile])

      try {
        setPendingFiles((prev) =>
          prev.map((pf) =>
            pf.id === pendingFile.id ? { ...pf, status: 'uploading' as const } : pf,
          ),
        )

        const result = await uploadFile(audioFile, modelName)
        setPendingFiles((prev) =>
          prev.map((pf) =>
            pf.id === pendingFile.id
              ? { ...pf, status: 'success' as const, uploadResult: result }
              : pf,
          ),
        )
      } catch (error: unknown) {
        let errorMessage = '音频上传失败'
        if (error instanceof Error) {
          errorMessage = error.message
        }
        reportError(errorMessage, ErrorType.NETWORK, ErrorSeverity.ERROR, {
          componentName: 'ChatInput',
          operation: 'uploadVoiceFile',
        })
        setPendingFiles((prev) =>
          prev.map((pf) =>
            pf.id === pendingFile.id
              ? { ...pf, status: 'error' as const, error: errorMessage }
              : pf,
          ),
        )
        setUploadError(errorMessage)
      }
    },
    [modelName, capabilities],
  )

  /** 提交（合并）当前未确认的临时语音文字到正文，并重置临时区间 作用：在用户键盘编辑、确认文字到达、停止录音等场景，把灰色临时文字 */
  const commitInterimVoice = useCallback(() => {
    interimVoiceStartRef.current = -1
  }, [])

  /** 处理语音实时临时识别结果（模型不支持音频时） 将临时文字实时追加/替换到 text 末尾，区间由 interimVoiceStartRef 标记， */
  const handleVoiceInterim = useCallback(
    (interimText: string) => {
      if (!interimText) return
      setText((prev) => {
        // 首次临时结果：记录起点，直接追加
        if (interimVoiceStartRef.current === -1) {
          const needSpace = prev && !prev.endsWith(' ')
          const start = prev.length + (needSpace ? 1 : 0)
          interimVoiceStartRef.current = start
          const newText = needSpace ? `${prev} ${interimText}` : `${prev}${interimText}`
          textRef.current = newText
          return newText
        }
        // 后续临时结果：替换上一段临时文字
        const base = prev.slice(0, interimVoiceStartRef.current)
        const newText = `${base}${interimText}`
        textRef.current = newText
        return newText
      })
      setTimeout(adjustTextareaHeight, 0)
    },
    [adjustTextareaHeight],
  )

  /** 处理语音转写完成（模型不支持音频时） final 到达时：若有未确认临时文字，先剔除临时区间，再追加确认文字； */
  const handleVoiceTranscriptionComplete = useCallback(
    (transcribedText: string) => {
      if (transcribedText.trim()) {
        setText((prev) => {
          // 剔除上一段临时识别文字（若存在），避免与 final 重复
          let base = prev
          if (interimVoiceStartRef.current !== -1) {
            base = prev.slice(0, interimVoiceStartRef.current)
          }
          const needSpace = base && !base.endsWith(' ')
          const newText = needSpace ? `${base} ${transcribedText}` : `${base}${transcribedText}`
          interimVoiceStartRef.current = -1
          textRef.current = newText
          if (draftKey) {
            useChatInputStore.getState().saveDraft(draftKey, newText)
          }
          return newText
        })
        setTimeout(adjustTextareaHeight, 0)
      }
    },
    [draftKey, adjustTextareaHeight],
  )

  /** 语音输入 Hook */
  const voiceInput = useVoiceInput({
    supportsAudio,
    language: 'zh-CN',
    continuous: true,
    onRecordingComplete: handleVoiceRecordingComplete,
    onTranscriptionComplete: handleVoiceTranscriptionComplete,
    onInterimResult: handleVoiceInterim,
    onError: (error) => {
      setUploadError(error.message)
    },
  })

  /** 处理文本变化 */
  const handleTextChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const newText = e.target.value
    // 用户键盘编辑后，临时语音文字区间可能失效，提交并清空标记
    commitInterimVoice()
    setText(newText)
    textRef.current = newText
    adjustTextareaHeight()
    if (draftKey) {
      useChatInputStore.getState().saveDraft(draftKey, newText)
    }
  }

  /** 处理键盘事件 */
  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    } else if (e.key === 'Escape' && isExpanded) {
      e.preventDefault()
      setIsExpanded(false)
    }
  }

  /** 上传文件到服务器 */
  const uploadFileAsync = useCallback(
    async (pendingFile: PendingFile) => {
      setPendingFiles((prev) =>
        prev.map((pf) => (pf.id === pendingFile.id ? { ...pf, status: 'uploading' as const } : pf)),
      )

      try {
        const result = await uploadFile(pendingFile.file, modelName)
        setPendingFiles((prev) =>
          prev.map((pf) =>
            pf.id === pendingFile.id
              ? { ...pf, status: 'success' as const, uploadResult: result }
              : pf,
          ),
        )
      } catch (error: unknown) {
        let errorMessage = '上传失败'

        if (error instanceof Error) {
          errorMessage = error.message
        } else if (typeof error === 'string') {
          errorMessage = error
        }

        reportError(errorMessage, ErrorType.NETWORK, ErrorSeverity.ERROR, {
          componentName: 'ChatInput',
          operation: 'uploadFile',
          fileName: pendingFile.file.name,
        })
        setPendingFiles((prev) =>
          prev.map((pf) =>
            pf.id === pendingFile.id
              ? { ...pf, status: 'error' as const, error: errorMessage }
              : pf,
          ),
        )
        setUploadError(errorMessage)
      }
    },
    [modelName],
  )

  /** 处理文件选择 */
  const handleFileSelect = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return
      if (!enableFileUpload) return

      setUploadError(null)

      const newPendingFiles: PendingFile[] = []
      for (let i = 0; i < files.length; i++) {
        const file = files[i]
        const validation = validateFile(file, capabilities)
        if (!validation.valid) {
          setUploadError(validation.error || '文件验证失败')
          continue
        }

        newPendingFiles.push({
          id: `${Date.now()}-${i}`,
          file,
          status: 'pending',
          previewUrl: file.type.startsWith('image/') ? URL.createObjectURL(file) : undefined,
        })
      }

      if (newPendingFiles.length === 0) return
      setPendingFiles((prev) => [...prev, ...newPendingFiles])

      for (const pf of newPendingFiles) {
        uploadFileAsync(pf)
      }
    },
    [enableFileUpload, uploadFileAsync, capabilities],
  )

  /** 移除附件 */
  const handleRemoveAttachment = useCallback((id: string) => {
    setAttachments((prev) => {
      const attachment = prev.find((a) => a.id === id)
      if (attachment?.previewUrl) {
        URL.revokeObjectURL(attachment.previewUrl)
      }
      return prev.filter((a) => a.id !== id)
    })

    setPendingFiles((prev) => {
      const file = prev.find((pf) => pf.id === id)
      if (file?.previewUrl) URL.revokeObjectURL(file.previewUrl)
      return prev.filter((pf) => pf.id !== id)
    })
  }, [])

  /** 发送消息 */
  const handleSend = useCallback(() => {
    const trimmedText = text.trim()

    const hasContent = trimmedText.length > 0
    const hasAttachments = attachments.length > 0
    const hasPendingFiles = pendingFiles.some((pf) => pf.status === 'success')

    if ((!hasContent && !hasAttachments && !hasPendingFiles) || disabled || isExecuting) {
      return
    }

    const allAttachments: Attachment[] = [...attachments]

    pendingFiles
      .filter((pf) => pf.status === 'success' && pf.uploadResult)
      .forEach((pf) => {
        allAttachments.push({
          id: pf.id,
          name: pf.uploadResult!.filename,
          type: pf.uploadResult!.mime_type,
          size: pf.file.size,
          url: pf.uploadResult!.url,
          status: 'completed',
        })
      })

    const params: SendMessageParams = {
      content: trimmedText,
      attachments: allAttachments.length > 0 ? allAttachments : undefined,
      enableThinking: currentThinkingMode?.enabled ?? false,
    }

    onSendMessage(params)
    setText('')
    textRef.current = ''
    interimVoiceStartRef.current = -1
    setAttachments([])
    setPendingFiles([])
    setUploadError(null)

    /** 发送后清除草稿 */
    if (draftKey) {
      useChatInputStore.getState().clearDraft(draftKey)
    }

    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
    setIsExpanded(false)
  }, [text, attachments, pendingFiles, disabled, isExecuting, onSendMessage, currentThinkingMode])

  /** 处理文件输入变化 */
  const handleFileInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      handleFileSelect(e.target.files)
      e.target.value = ''
    },
    [handleFileSelect],
  )

  /** 触发文件选择 */
  const triggerFileSelect = useCallback(() => {
    fileInputRef.current?.click()
  }, [])

  /** 拖拽事件处理 */
  const handleDragOver = useCallback(
    (e: React.DragEvent) => {
      if (!enableDragDrop || !inputCapabilities.canDragDrop) return
      e.preventDefault()
      setIsDragging(true)
    },
    [enableDragDrop, inputCapabilities.canDragDrop],
  )

  const handleDragLeave = useCallback(
    (e: React.DragEvent) => {
      if (!enableDragDrop || !inputCapabilities.canDragDrop) return
      e.preventDefault()
      setIsDragging(false)
    },
    [enableDragDrop, inputCapabilities.canDragDrop],
  )

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      if (!enableDragDrop || !inputCapabilities.canDragDrop) return
      e.preventDefault()
      setIsDragging(false)
      handleFileSelect(e.dataTransfer.files)
    },
    [enableDragDrop, inputCapabilities.canDragDrop, handleFileSelect],
  )

  /** 粘贴事件处理 */
  const handlePaste = useCallback(
    (e: React.ClipboardEvent) => {
      if (!inputCapabilities.canPasteImage) return

      const items = e.clipboardData.items
      const imageFiles: File[] = []

      for (const item of items) {
        if (item.type.startsWith('image/')) {
          const file = item.getAsFile()
          if (file) {
            imageFiles.push(file)
          }
        }
      }

      if (imageFiles.length > 0) {
        e.preventDefault()
        const dataTransfer = new DataTransfer()
        imageFiles.forEach((file) => dataTransfer.items.add(file))
        handleFileSelect(dataTransfer.files)
      }
    },
    [inputCapabilities.canPasteImage, handleFileSelect],
  )

  /** 清理预览 URL */
  useEffect(() => {
    return () => {
      pendingFiles.forEach((pf) => {
        if (pf.previewUrl) URL.revokeObjectURL(pf.previewUrl)
      })
    }
  }, [pendingFiles])

  /** 消费外部插入文本（如引用功能注入的文本） */
  useEffect(() => {
    const processInsert = (insertText: string) => {
      const currentText = textRef.current
      const newText = currentText ? `${currentText}\n${insertText}` : insertText
      setText(newText)
      textRef.current = newText
      useChatInputStore.getState().consumeInsert()
      if (draftKey) {
        useChatInputStore.getState().saveDraft(draftKey, newText)
      }
      setTimeout(() => {
        adjustTextareaHeight()
        textareaRef.current?.focus()
      }, 0)
    }

    const { pendingInsert } = useChatInputStore.getState()
    if (pendingInsert) {
      processInsert(pendingInsert)
    }

    const unsubscribe = useChatInputStore.subscribe((state) => {
      if (!state.pendingInsert) return
      processInsert(state.pendingInsert)
    })
    return unsubscribe
  }, [draftKey, adjustTextareaHeight])

  /** 组件卸载前保存当前文本到草稿 */
  useEffect(() => {
    return () => {
      if (draftKey) {
        useChatInputStore.getState().saveDraft(draftKey, textRef.current)
      }
    }
  }, [draftKey])

  const isUploading = pendingFiles.some((pf) => pf.status === 'uploading')
  const isCompactMode = mode === 'compact'

  const canSend =
    (text.trim() || attachments.length > 0 || pendingFiles.some((pf) => pf.status === 'success')) &&
    !disabled &&
    !isExecuting &&
    !isUploading

  return (
    <div
      className={cn(
        'w-full',
        className,
        isDragging && enableDragDrop && inputCapabilities.canDragDrop
          ? 'ring-primary ring-2 ring-offset-2'
          : '',
      )}
      data-testid="chat-input"
      role="region"
      aria-label="聊天输入区域"
      onDragOver={enableDragDrop && inputCapabilities.canDragDrop ? handleDragOver : undefined}
      onDragLeave={enableDragDrop && inputCapabilities.canDragDrop ? handleDragLeave : undefined}
      onDrop={enableDragDrop && inputCapabilities.canDragDrop ? handleDrop : undefined}
    >
      {/* 上传错误提示 */}
      {uploadError && (
        <div
          id="upload-error"
          role="alert"
          className="text-destructive bg-destructive/10 mb-3 flex items-center gap-2 rounded-xl p-2 text-sm"
        >
          <AlertCircle size={16} className="flex-shrink-0" />
          <span className="flex-1">{uploadError}</span>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 w-6 rounded-lg p-0"
            onClick={() => setUploadError(null)}
            aria-label="关闭错误提示"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      )}

      {/* 输入框容器 */}
      <div
        className={cn(
          'relative rounded-2xl border',
          'bg-background/80 border-border/50',
          'shadow-sm transition-shadow duration-200',
          isExpanded
            ? 'shadow-md focus-within:border-primary/50'
            : 'hover:shadow-md focus-within:ring-ring/50 focus-within:border-primary/50 focus-within:ring-2',
        )}
      >
        {/* 展开/收起编辑器按钮：固定在输入框右上角，悬浮于文本之上 */}
        <Button
          variant="ghost"
          size="icon"
          className="text-muted-foreground hover:text-foreground hover:bg-muted absolute top-2 right-2 z-10 h-8 w-8 rounded-lg"
          onClick={() => setIsExpanded((v) => !v)}
          aria-label={isExpanded ? '收起编辑器' : '展开编辑器'}
          aria-expanded={isExpanded}
          title={isExpanded ? '收起 (Esc)' : '展开为大编辑器'}
          data-testid="chat-input-expand-toggle"
        >
          {isExpanded ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
        </Button>
        {(attachments.length > 0 || pendingFiles.length > 0) && (
          <div className="flex flex-wrap gap-2 px-3 pt-3 pb-2">
            {attachments.map((attachment) => (
              <AttachmentPreview
                key={attachment.id}
                attachment={attachment}
                onRemove={() => handleRemoveAttachment(attachment.id)}
              />
            ))}
            {pendingFiles.map((pendingFile) => (
              <AttachmentPreview
                key={pendingFile.id}
                attachment={pendingFile}
                onRemove={() => handleRemoveAttachment(pendingFile.id)}
              />
            ))}
          </div>
        )}

        {/* 录音状态条（容器内顶部，与附件预览区结构对称；不改变与上方消息列表的距离） */}
        {voiceInput.isRecording && (
          <div className="flex items-center justify-between gap-2 px-3 pt-3 pb-1">
            <div className="flex items-center gap-2">
              <span
                className="bg-status-error inline-block h-2 w-2 rounded-full"
                style={{ animation: 'voice-pulse-core 1.2s ease-in-out infinite' }}
              />
              <span className="text-muted-foreground text-xs font-medium">
                {voiceInput.mode === 'server-asr' ? '服务器识别中…' : '正在聆听…'}
              </span>
            </div>
            <span className="text-muted-foreground/70 font-mono text-xs tabular-nums">
              {formatDuration(voiceInput.recordingDuration)}
            </span>
          </div>
        )}

        {/* 文本输入框 */}
        <textarea
          ref={textareaRef}
          value={text}
          onChange={handleTextChange}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={
            isExecuting
              ? mode === 'smart'
                ? '执行中...'
                : '正在生成回复...'
              : isDragging && enableDragDrop && inputCapabilities.canDragDrop
                ? '松开鼠标上传文件'
                : enableFileUpload && !isCompactMode && inputCapabilities.showAttachmentButton
                  ? 'Enter 发送 · Shift+Enter 换行 · 支持拖拽上传'
                  : 'Enter 发送 · Shift+Enter 换行'
          }
          disabled={disabled || isExecuting}
          rows={1}
          data-testid="chat-input-textarea"
          aria-label="消息输入"
          aria-describedby={uploadError ? 'upload-error' : undefined}
          className={cn(
            'w-full resize-none',
            'pl-3 pt-3 pb-2 pr-10',
            'border-0 outline-none focus:outline-none',
            'disabled:cursor-not-allowed disabled:opacity-50',
            isExpanded ? 'max-h-[80vh] min-h-[44px]' : 'max-h-[33vh] min-h-[44px]',
            'text-foreground placeholder:text-muted-foreground/40',
            'bg-transparent',
          )}
        />

        {/* 底部工具栏 */}
        <div className="flex items-center justify-between gap-2 px-3 pb-3">
          <div className="flex items-center gap-1.5">
            {/* 附件按钮 */}
            {enableFileUpload && !isCompactMode && inputCapabilities.showAttachmentButton && (
              <Button
                variant="ghost"
                size="icon"
                className="text-muted-foreground hover:text-foreground hover:bg-muted h-11 w-11 rounded-lg sm:h-8 sm:w-8"
                onClick={triggerFileSelect}
                disabled={disabled || isExecuting}
                title="添加附件"
                aria-label="添加附件"
              >
                <Paperclip className="h-4 w-4" />
              </Button>
            )}

            {/* 语音输入按钮 */}
            {!isCompactMode && voiceInput.isSupported && (
              <VoiceInputButton
                disabled={disabled || isExecuting}
                state={voiceInput.state}
                error={voiceInput.error}
                recordingDuration={voiceInput.recordingDuration}
                aria-label={voiceInput.isRecording ? '停止语音输入' : '开始语音输入'}
                onClick={() => {
                  if (voiceInput.isRecording) {
                    // 停止前提交未确认的临时文字（保留为正文），并保存草稿
                    if (interimVoiceStartRef.current !== -1) {
                      commitInterimVoice()
                      if (draftKey) {
                        useChatInputStore.getState().saveDraft(draftKey, textRef.current)
                      }
                    }
                    voiceInput.stopRecording()
                  } else {
                    interimVoiceStartRef.current = -1
                    voiceInput.startRecording()
                  }
                }}
              />
            )}

            {/* 思考模式切换按钮 */}
            {enableThinkingMode && !isCompactMode && (
              <ThinkingModeToggle
                currentModel={modelName || 'unknown'}
                thinkingMode={currentThinkingMode}
                onToggle={currentToggleThinkingMode}
                disabled={disabled || isExecuting || !modelName || modelName === 'unknown'}
              />
            )}

            {/* 模型名和 Token 统计：模型无效时如实显示「模型无效」，不用默认值冒充 */}
            {modelName ? (
              <div className="bg-primary/10 border-primary/20 hidden h-8 items-center gap-2 rounded-lg border px-3 text-xs sm:flex">
                <Database className="text-primary h-3.5 w-3.5" />
                <span className="text-primary font-semibold">{modelName}</span>
                {maxTokens > 0 && (
                  <>
                    <span className="text-primary/40">|</span>
                    <div className="bg-primary/20 h-1.5 w-20 overflow-hidden rounded-full">
                      <div
                        className={cn(
                          'h-full rounded-full transition-all duration-300',
                          currentTokenUsage / maxTokens >= 0.9
                            ? 'bg-status-error'
                            : currentTokenUsage / maxTokens >= 0.7
                              ? 'bg-status-warning'
                              : 'bg-status-success',
                        )}
                        style={{
                          width: `${Math.min((currentTokenUsage / maxTokens) * 100, 100)}%`,
                        }}
                      />
                    </div>
                    <span className="text-primary font-medium">
                      {formatNumber(currentTokenUsage)}
                    </span>
                    <span className="text-primary/50">/</span>
                    <span className="text-primary/70">{formatNumber(maxTokens)}</span>
                  </>
                )}
              </div>
            ) : (
              <div className="hidden h-8 items-center gap-2 rounded-lg border border-muted/20 px-3 text-xs text-muted-foreground sm:flex">
                <AlertCircle className="h-3.5 w-3.5" />
                <span>模型无效</span>
              </div>
            )}
          </div>

          {/* 发送/停止按钮 */}
          {isExecuting && onStopGenerate ? (
            <Button
              variant="destructive"
              size="icon"
              className="h-11 w-11 rounded-lg sm:h-8 sm:w-8"
              onClick={onStopGenerate}
              title="停止生成"
              aria-label="停止生成"
            >
              <Square className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              variant="default"
              size="icon"
              className={cn(
                'h-11 w-11 rounded-lg transition-all duration-200 sm:h-8 sm:w-8',
                canSend
                  ? 'bg-primary hover:bg-primary/90 shadow-sm'
                  : 'bg-muted text-muted-foreground',
              )}
              onClick={handleSend}
              disabled={!canSend}
              title="发送消息"
              aria-label="发送消息"
              data-testid="chat-send-button"
            >
              <Send className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      {/* 隐藏的文件输入 */}
      {enableFileUpload && !isCompactMode && inputCapabilities.showAttachmentButton && (
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={handleFileInputChange}
        />
      )}
    </div>
  )
}
