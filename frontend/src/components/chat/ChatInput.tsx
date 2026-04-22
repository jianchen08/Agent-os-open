/**
 * 统一的聊天输入组件
 *
 * 支持三种模式：
 * - full: 完整功能（文件上传、拖拽、附件预览）
 * - compact: 简化版（仅文本输入）
 * - smart: 智能版（根据执行状态切换按钮）
 *
 * 功能特性：
 * - 文本输入（Enter 发送，Shift+Enter 换行）
 * - 文件上传（支持图片和文档）
 * - 拖拽上传
 * - 附件预览和删除
 * - 上传状态管理
 * - 错误处理
 * - 执行状态控制
 */

import { useModelCapabilities } from '@/hooks/useModelCapabilities'
import { useVoiceInput } from '@/hooks/useVoiceInput'
import { cn } from '@/lib/utils'
import {
    ErrorSeverity,
    ErrorType,
    reportError,
} from '@/services/errorReporting'
import {
    AlertCircle,
    Database,
    File as FileIcon,
    Image as ImageIcon,
    Loader2,
    Paperclip,
    Send,
    Square,
    X,
} from 'lucide-react'
import {
    useCallback,
    useEffect,
    useRef,
    useState,
    type KeyboardEvent,
} from 'react'
import { uploadFile, validateFile } from '@/services/api/files'
import type { ThinkingModeState } from '@/types/thinkingMode'
import { Button } from '@/components/ui/button'
import { ThinkingModeToggle } from './ThinkingModeToggle'
import { VoiceInputButton } from './VoiceInputButton'
import type {
    Attachment,
    ChatInputProps,
    PendingFile,
    SendMessageParams,
} from './types'

/**
 * 格式化文件大小
 */
const formatFileSize = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/**
 * 格式化数字（添加千位分隔符）
 */
const formatNumber = (num: number): string => {
  return num.toLocaleString('en-US')
}

/**
 * 附件预览组件
 */
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
  const status = isPendingFile
    ? attachment.status
    : (attachment as Attachment).status
  const fileName = isPendingFile ? attachment.file.name : attachment.name
  const fileSize = isPendingFile ? attachment.file.size : attachment.size
  const previewUrl = isPendingFile
    ? attachment.previewUrl
    : attachment.previewUrl

  return (
    <div
      className={cn(
        'relative group flex items-center gap-2 p-2 rounded-xl transition-all duration-200',
        status === 'error'
          ? 'bg-destructive/10 border border-destructive/50'
          : 'bg-muted/50 border border-border/30 hover:border-border/50 hover:shadow-sm'
      )}
    >
      {/* 预览图标/缩略图 */}
      {previewUrl ? (
        <img
          src={previewUrl}
          alt={fileName}
          className="w-10 h-10 object-cover rounded-lg"
        />
      ) : (
        <div className="w-10 h-10 flex items-center justify-center bg-background/80 rounded-lg">
          {isImage ? (
            <ImageIcon className="w-5 h-5 text-muted-foreground" />
          ) : (
            <FileIcon className="w-5 h-5 text-muted-foreground" />
          )}
        </div>
      )}

      {/* 文件信息 */}
      <div className="flex-1 min-w-0">
        <div className="text-sm truncate font-medium">{fileName}</div>
        <div className="text-xs text-muted-foreground">
          {formatFileSize(fileSize)}
        </div>
      </div>

      {/* 上传状态 */}
      {status === 'uploading' && (
        <Loader2 className="w-4 h-4 animate-spin text-primary" />
      )}
      {status === 'error' && (
        <AlertCircle className="w-4 h-4 text-destructive" />
      )}

      {/* 删除按钮 */}
      <Button
        variant="ghost"
        size="sm"
        className="h-6 w-6 p-0 opacity-0 group-hover:opacity-100 rounded-lg hover:bg-destructive/10 hover:text-destructive"
        onClick={onRemove}
        disabled={status === 'uploading'}
      >
        <X className="w-4 h-4" />
      </Button>
    </div>
  )
}

/**
 * 统一的聊天输入组件
 */
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
  enableThinkingMode = false,
  thinkingMode,
  toggleThinkingMode,
  className = '',
}: ChatInputProps) => {
  /** 获取模型能力配置 */
  const { inputCapabilities } = useModelCapabilities(modelName)

  const [text, setText] = useState('')
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([])
  const [isDragging, setIsDragging] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)

  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  /** 思考模式状态：优先使用外部传入的值 */
  const currentThinkingMode: ThinkingModeState = thinkingMode || {
    enabled: false,
    currentModel: modelName || '',
    switching: false,
  }
  const currentToggleThinkingMode = toggleThinkingMode || (async (_enabled: boolean) => {
    // 思考模式切换由外部控制
  })

  /** 判断是否正在执行/生成 */
  const isExecuting =
    mode === 'smart' ? executionState === 'running' : isGenerating

  /** 获取模型是否支持音频 */
  const { capabilities } = useModelCapabilities(modelName)
  const supportsAudio = capabilities?.supportsAudio ?? false

  /**
   * 处理语音录音完成（模型支持音频时）
   */
  const handleVoiceRecordingComplete = useCallback(
    async (audioBlob: Blob) => {
      const timestamp = Date.now()
      const audioFile = new File([audioBlob], `voice_${timestamp}.webm`, {
        type: audioBlob.type || 'audio/webm',
      })

      const validation = validateFile(audioFile)
      if (!validation.valid) {
        setUploadError(validation.error || '音频文件验证失败')
        return
      }

      const pendingFile: PendingFile = {
        id: `voice-${timestamp}`,
        file: audioFile,
        status: 'pending',
      }

      setPendingFiles(prev => [...prev, pendingFile])

      try {
        setPendingFiles(prev =>
          prev.map(pf =>
            pf.id === pendingFile.id
              ? { ...pf, status: 'uploading' as const }
              : pf
          )
        )

        const result = await uploadFile(audioFile, modelName)
        setPendingFiles(prev =>
          prev.map(pf =>
            pf.id === pendingFile.id
              ? { ...pf, status: 'success' as const, uploadResult: result }
              : pf
          )
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
        setPendingFiles(prev =>
          prev.map(pf =>
            pf.id === pendingFile.id
              ? { ...pf, status: 'error' as const, error: errorMessage }
              : pf
          )
        )
        setUploadError(errorMessage)
      }
    },
    [modelName]
  )

  /**
   * 处理语音转写完成（模型不支持音频时）
   */
  const handleVoiceTranscriptionComplete = useCallback(
    (transcribedText: string) => {
      if (transcribedText.trim()) {
        setText(prev => (prev ? `${prev} ${transcribedText}` : transcribedText))
        setTimeout(() => {
          const textarea = textareaRef.current
          if (textarea) {
            textarea.style.height = 'auto'
            textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`
          }
        }, 0)
      }
    },
    []
  )

  /** 语音输入 Hook */
  const voiceInput = useVoiceInput({
    supportsAudio,
    language: 'zh-CN',
    continuous: true,
    onRecordingComplete: handleVoiceRecordingComplete,
    onTranscriptionComplete: handleVoiceTranscriptionComplete,
    onError: (error) => {
      setUploadError(error.message)
    },
  })

  /** 自动调整文本框高度 */
  const adjustTextareaHeight = useCallback(() => {
    const textarea = textareaRef.current
    if (textarea) {
      textarea.style.height = 'auto'
      textarea.style.height = `${Math.min(textarea.scrollHeight, 200)}px`
    }
  }, [])

  /** 处理文本变化 */
  const handleTextChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value)
    adjustTextareaHeight()
  }

  /** 处理键盘事件 */
  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  /** 上传文件到服务器 */
  const uploadFileAsync = useCallback(
    async (pendingFile: PendingFile) => {
      setPendingFiles(prev =>
        prev.map(pf =>
          pf.id === pendingFile.id
            ? { ...pf, status: 'uploading' as const }
            : pf
        )
      )

      try {
        const result = await uploadFile(pendingFile.file, modelName)
        setPendingFiles(prev =>
          prev.map(pf =>
            pf.id === pendingFile.id
              ? { ...pf, status: 'success' as const, uploadResult: result }
              : pf
          )
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
        setPendingFiles(prev =>
          prev.map(pf =>
            pf.id === pendingFile.id
              ? { ...pf, status: 'error' as const, error: errorMessage }
              : pf
          )
        )
        setUploadError(errorMessage)
      }
    },
    [modelName]
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
        const validation = validateFile(file)
        if (!validation.valid) {
          setUploadError(validation.error || '文件验证失败')
          continue
        }

        newPendingFiles.push({
          id: `${Date.now()}-${i}`,
          file,
          status: 'pending',
          previewUrl: file.type.startsWith('image/')
            ? URL.createObjectURL(file)
            : undefined,
        })
      }

      if (newPendingFiles.length === 0) return
      setPendingFiles(prev => [...prev, ...newPendingFiles])

      for (const pf of newPendingFiles) {
        uploadFileAsync(pf)
      }
    },
    [enableFileUpload, uploadFileAsync]
  )

  /** 移除附件 */
  const handleRemoveAttachment = useCallback((id: string) => {
    setAttachments(prev => {
      const attachment = prev.find(a => a.id === id)
      if (attachment?.previewUrl) {
        URL.revokeObjectURL(attachment.previewUrl)
      }
      return prev.filter(a => a.id !== id)
    })

    setPendingFiles(prev => {
      const file = prev.find(pf => pf.id === id)
      if (file?.previewUrl) URL.revokeObjectURL(file.previewUrl)
      return prev.filter(pf => pf.id !== id)
    })
  }, [])

  /** 发送消息 */
  const handleSend = useCallback(() => {
    const trimmedText = text.trim()

    const hasContent = trimmedText.length > 0
    const hasAttachments = attachments.length > 0
    const hasPendingFiles = pendingFiles.some(pf => pf.status === 'success')

    if (
      (!hasContent && !hasAttachments && !hasPendingFiles) ||
      disabled ||
      isExecuting
    ) {
      return
    }

    const allAttachments: Attachment[] = [...attachments]

    pendingFiles
      .filter(pf => pf.status === 'success' && pf.uploadResult)
      .forEach(pf => {
        allAttachments.push({
          id: pf.id,
          name: pf.uploadResult!.filename,
          type: pf.uploadResult!.mime_type,
          size: pf.file.size,
          url: pf.uploadResult!.file_id,
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
    setAttachments([])
    setPendingFiles([])
    setUploadError(null)

    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
  }, [
    text,
    attachments,
    pendingFiles,
    disabled,
    isExecuting,
    onSendMessage,
    currentThinkingMode,
  ])

  /** 处理文件输入变化 */
  const handleFileInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      handleFileSelect(e.target.files)
      e.target.value = ''
    },
    [handleFileSelect]
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
    [enableDragDrop, inputCapabilities.canDragDrop]
  )

  const handleDragLeave = useCallback(
    (e: React.DragEvent) => {
      if (!enableDragDrop || !inputCapabilities.canDragDrop) return
      e.preventDefault()
      setIsDragging(false)
    },
    [enableDragDrop, inputCapabilities.canDragDrop]
  )

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      if (!enableDragDrop || !inputCapabilities.canDragDrop) return
      e.preventDefault()
      setIsDragging(false)
      handleFileSelect(e.dataTransfer.files)
    },
    [enableDragDrop, inputCapabilities.canDragDrop, handleFileSelect]
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
        imageFiles.forEach(file => dataTransfer.items.add(file))
        handleFileSelect(dataTransfer.files)
      }
    },
    [inputCapabilities.canPasteImage, handleFileSelect]
  )

  /** 清理预览 URL */
  useEffect(() => {
    return () => {
      pendingFiles.forEach(pf => {
        if (pf.previewUrl) URL.revokeObjectURL(pf.previewUrl)
      })
    }
  }, [pendingFiles])

  const isUploading = pendingFiles.some(pf => pf.status === 'uploading')
  const isCompactMode = mode === 'compact'

  const canSend =
    (text.trim() ||
      attachments.length > 0 ||
      pendingFiles.some(pf => pf.status === 'success')) &&
    !disabled &&
    !isExecuting &&
    !isUploading

  return (
    <div
      className={cn(
        'w-full',
        className,
        isDragging && enableDragDrop && inputCapabilities.canDragDrop ? 'ring-2 ring-primary ring-offset-2' : ''
      )}
      data-testid="chat-input"
      onDragOver={enableDragDrop && inputCapabilities.canDragDrop ? handleDragOver : undefined}
      onDragLeave={enableDragDrop && inputCapabilities.canDragDrop ? handleDragLeave : undefined}
      onDrop={enableDragDrop && inputCapabilities.canDragDrop ? handleDrop : undefined}
    >
      {/* 上传错误提示 */}
      {uploadError && (
        <div className="p-2 flex items-center gap-2 text-sm text-destructive bg-destructive/10 mb-3 rounded-xl">
          <AlertCircle size={16} className="flex-shrink-0" />
          <span className="flex-1">{uploadError}</span>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 w-6 p-0 rounded-lg"
            onClick={() => setUploadError(null)}
          >
            <X className="w-4 h-4" />
          </Button>
        </div>
      )}

      {/* 输入框容器 */}
      <div
          className={cn(
            'rounded-2xl',
            'bg-background/80 border border-border/50',
            'shadow-sm hover:shadow-md transition-shadow duration-200',
            'focus-within:ring-2 focus-within:ring-ring/50 focus-within:border-primary/50'
          )}
        >
        {/* 附件预览区 */}
        {(attachments.length > 0 || pendingFiles.length > 0) && (
          <div className="px-3 pt-3 pb-2 flex flex-wrap gap-2">
            {attachments.map(attachment => (
              <AttachmentPreview
                key={attachment.id}
                attachment={attachment}
                onRemove={() => handleRemoveAttachment(attachment.id)}
              />
            ))}
            {pendingFiles.map(pendingFile => (
              <AttachmentPreview
                key={pendingFile.id}
                attachment={pendingFile}
                onRemove={() => handleRemoveAttachment(pendingFile.id)}
              />
            ))}
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
          className={cn(
            'w-full resize-none',
            'px-3 pt-3 pb-2',
            'focus:outline-none border-0 outline-none',
            'disabled:cursor-not-allowed disabled:opacity-50',
            'min-h-[44px] max-h-[200px]',
            'text-foreground placeholder:text-muted-foreground/40',
            'bg-transparent'
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
                className="h-8 w-8 text-muted-foreground hover:text-foreground hover:bg-muted rounded-lg"
                onClick={triggerFileSelect}
                disabled={disabled || isExecuting}
                title="添加附件"
              >
                <Paperclip className="w-4 h-4" />
              </Button>
            )}

            {/* 语音输入按钮 */}
            {!isCompactMode && voiceInput.isSupported && (
              <VoiceInputButton
                disabled={disabled || isExecuting}
                state={voiceInput.state}
                error={voiceInput.error}
                onClick={() => {
                  if (voiceInput.isRecording) {
                    voiceInput.stopRecording()
                  } else {
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
                disabled={
                  disabled || isExecuting || !modelName || modelName === 'unknown'
                }
              />
            )}

            {/* 模型名和 Token 统计 */}
            {modelName && (
              <div className="flex items-center gap-2 h-8 px-3 rounded-lg bg-primary/10 border border-primary/20 text-xs">
                <Database className="w-3.5 h-3.5 text-primary" />
                <span className="font-semibold text-primary">{modelName}</span>
                {maxTokens > 0 && (
                  <>
                    <span className="text-primary/40">|</span>
                    <div className="w-20 h-1.5 bg-primary/20 rounded-full overflow-hidden">
                      <div
                        className={cn(
                          'h-full rounded-full transition-all duration-300',
                          (currentTokenUsage / maxTokens) >= 0.9
                            ? 'bg-red-500'
                            : (currentTokenUsage / maxTokens) >= 0.7
                              ? 'bg-amber-500'
                              : 'bg-emerald-500'
                        )}
                        style={{ width: `${Math.min((currentTokenUsage / maxTokens) * 100, 100)}%` }}
                      />
                    </div>
                    <span className="font-medium text-primary">
                      {formatNumber(currentTokenUsage)}
                    </span>
                    <span className="text-primary/50">/</span>
                    <span className="text-primary/70">
                      {formatNumber(maxTokens)}
                    </span>
                  </>
                )}
              </div>
            )}
          </div>

          {/* 发送/停止按钮 */}
          {isExecuting && onStopGenerate ? (
            <Button
              variant="destructive"
              size="icon"
              className="h-8 w-8 rounded-lg"
              onClick={onStopGenerate}
              title="停止生成"
            >
              <Square className="w-4 h-4" />
            </Button>
          ) : (
            <Button
              variant="default"
              size="icon"
              className={cn(
                'h-8 w-8 rounded-lg transition-all duration-200',
                canSend
                  ? 'bg-primary hover:bg-primary/90 shadow-sm'
                  : 'bg-muted text-muted-foreground'
              )}
              onClick={handleSend}
              disabled={!canSend}
              title="发送消息"
              data-testid="chat-send-button"
            >
              <Send className="w-4 h-4" />
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
          accept={inputCapabilities.acceptedFileTypes || 'image/*,.pdf,.doc,.docx,.txt,.md'}
        />
      )}
    </div>
  )
}
