/** 统一的聊天输入组件 支持三种模式： */

import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react'
import {
  AlertCircle,
  Maximize2,
  Minimize2,
  Paperclip,
  X,
} from '@/assets/icons'
import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { ContextUsageWidget } from '@/components/schema/widgets/ContextUsageWidget'
import { Button } from '@/components/ui/button'
import { useControlledSlotBridge } from '@/hooks/useControlledSlotBridge'
import { useIsMobileViewport } from '@/hooks/useIsMobileViewport'
import { useModelCapabilities } from '@/hooks/useModelCapabilities'
import { useSendOutcomeConsumer } from '@/hooks/useSendOutcomeConsumer'
import { useVoiceInput } from '@/hooks/useVoiceInput'
import { cn } from '@/lib/utils'
import { uploadFile, validateFile } from '@/services/api/files'
import { ErrorSeverity, ErrorType, reportError } from '@/services/errorReporting'
import {
  clearSessionAgentBinding,
  readSessionAgentBinding,
  type SessionAgentBinding,
} from '@/services/modeSessionBinder'
import { getReferenceProviders } from '@/services/references'
import { useChatInputStore } from '@/stores/chatInputStore'
import { usePersonaPossessStore } from '@/stores/personaPossessStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { type ThinkingStrength } from '@/types/thinkingMode'
import { ChatInputActions } from './ChatInputActions'
import { AttachmentPreview } from './ChatInputAttachmentPreview'
import { ChatInputPossessChip } from './ChatInputPossessChip'
import { CitationCardRow } from './CitationCardRow'
import { ComposerRecordingBanner } from './ComposerRecordingBanner'
import { composerPlaceholder, composeSendParams } from './composerSend'
import { ComposerSendButton } from './ComposerSendButton'
import { SessionAgentIndicator } from './SessionAgentIndicator'
import { VoiceInputButton } from './VoiceInputButton'
import type { Attachment, ChatInputProps, PendingFile } from './types'

/** 统一的聊天输入组件 */
export const ChatInput = ({
  mode = 'full',
  disabled = false,
  isGenerating = false,
  sendInFlight = false,
  executionState = 'idle',
  onSendMessage,
  onStopGenerate,
  enableFileUpload = true,
  enableDragDrop = true,
  modelName,
  enableThinkingMode = false,
  thinkingStrength: _externalThinkingStrength,
  onThinkingStrengthChange,
  modelId,
  onModelIdChange,
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
  /** 输入区错误提示（上传失败/发送未受理/发送判死）：落在操作点、可手动关闭，
   *  下一次受理发送自动清除 */
  const [inputError, setInputError] = useState<string | null>(null)
  const [isExpanded, setIsExpanded] = useState(false)
  /** 附身档（mode.possess 桥写入，全局态不按会话分）：null = 未附身 */
  const possessed = usePersonaPossessStore((s) => s.possessed)
  /** 会话绑定（mode.session 桥写入会话执行选项，按活跃会话分）：执行者绑定
   *  （指示条）随活跃会话切换重读。模式绑定不再有输入行选择器（R10③ 收拢：
   *  会话级选择，模式会话经侧栏模式面板/浏览智能体等选择处出生）。 */
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const [sessionAgent, setSessionAgent] = useState<SessionAgentBinding | null>(null)
  useEffect(() => {
    if (!activeSessionId) {
      setSessionAgent(null)
      return
    }
    setSessionAgent(readSessionAgentBinding(activeSessionId))
  }, [activeSessionId])
  /** 退出绑定会话：清该会话执行选项的 agentId（发送链随之不带身份） */
  const handleExitSessionBinding = useCallback(() => {
    if (!activeSessionId) return
    try {
      clearSessionAgentBinding(activeSessionId)
    } catch (e) {
      // 本地存储不可用属环境故障：落在操作点显式提示，不静默假退出
      setInputError(`退出会话身份绑定失败：${e instanceof Error ? e.message : String(e)}`)
      return
    }
    setSessionAgent(null)
  }, [activeSessionId])

  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  /** 追踪最新文本值，用于组件卸载时保存草稿 */
  const textRef = useRef(text)
  /** 语音实时识别：临时文字在 text 中的起始偏移量，-1 表示无未确认临时文字 */
  const interimVoiceStartRef = useRef(-1)

  /** 思考选择：优先使用外部传入值（随标签路由自动变化），'' = 未选择（消息
   *  不带思考参数，模型按 default_params 运行） */
  const currentThinkingStrength: ThinkingStrength = _externalThinkingStrength ?? ''
  const handleStrengthChange = useCallback(
    (strength: ThinkingStrength) => {
      onThinkingStrengthChange?.(strength)
    },
    [onThinkingStrengthChange],
  )

  // 受控双向绑定桥（widget 化 G4）：宿主向声明组件注入 value/onChange——
  // 字段名、值形态由钩子统一处理，无需手写 shape
  const thinkingStrengthBridge = useControlledSlotBridge('thinking_strength', {
    field: 'strength',
    get: () => currentThinkingStrength,
    set: (_f, v) => handleStrengthChange(v as ThinkingStrength),
    // 惰性求值：isExecuting 在下方声明，渲染期回调时才读取（规避 TDZ）
    extra: () => ({ disabled: disabled || isExecuting || !modelName || modelName === 'unknown' }),
  })

  /** 显式模型选择：'' = 未选择（选择器占位显示配置模型，帧不带 model_id） */
  const currentModelId: string = modelId ?? ''
  const handleModelIdChange = useCallback(
    (modelIdValue: string) => {
      onModelIdChange?.(modelIdValue)
    },
    [onModelIdChange],
  )
  const modelIdBridge = useControlledSlotBridge('model_id', {
    field: 'model_id',
    get: () => currentModelId,
    set: (_f, v) => handleModelIdChange((v as string) ?? ''),
    // fallbackModelName：新会话默认模型显示面（agent 头/系统默认，ChatContainer
    // 解析）——选择器声明组件据此在无显式选择、state 行未出时常驻显示默认模型
    extra: () => ({
      disabled: disabled || isExecuting,
      fallbackModelName: modelName ?? '',
    }),
  })

  /** 必须声明在使用它的回调（handleVoiceInterim / handleVoiceTranscriptionComplete 等）之前，
   *  否则在依赖数组中访问会触发 TDZ（Cannot access ... before initialization）。
   *  非展开态：随内容自适应，上限为视口高度的 1/3；展开态：固定高度，不随内容变化。*/
  const adjustTextareaHeight = useCallback(() => {
    if (isExpanded) return
    const textarea = textareaRef.current
    if (textarea) {
      // 工作区全屏期间本输入框 CSS 隐藏（display:none，scrollHeight 恒 0）：
      // 此时外部插入/浮窗镜像同步仍会写文本，若照常计算会把高度打成 0，
      // 退出全屏后呈现塌陷输入框——跳过，由退出后的下一次输入重算。
      if (useUIStore.getState().workspaceFullscreen) return
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
        setInputError(validation.error || '音频文件验证失败')
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
        reportError(errorMessage, {
          type: ErrorType.NETWORK,
          severity: ErrorSeverity.ERROR,
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
        setInputError(errorMessage)
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
      setInputError(error.message)
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

        reportError(errorMessage, {
          type: ErrorType.NETWORK,
          severity: ErrorSeverity.ERROR,
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
        setInputError(errorMessage)
      }
    },
    [modelName],
  )

  /** 处理文件选择 */
  const handleFileSelect = useCallback(
    async (files: FileList | null) => {
      if (!files || files.length === 0) return
      if (!enableFileUpload) return

      setInputError(null)

      const newPendingFiles: PendingFile[] = []
      for (let i = 0; i < files.length; i++) {
        const file = files[i]
        const validation = validateFile(file, capabilities)
        if (!validation.valid) {
          setInputError(validation.error || '文件验证失败')
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

  /** 本次 handleSend 是否由远程发送（全屏浮窗）触发：拒绝分支据此镜像反馈到
   *  store（浮窗可见）；主输入框自身路径不写该字段 */
  const remoteSendActiveRef = useRef(false)

  /** 发送拒绝的操作点报错；远程发送（全屏浮窗）触发时镜像到 store 供浮窗显示
   *  （浮窗发送失败路径与主输入框一致，不静默） */
  const reportSendRefusal = useCallback((message: string) => {
    setInputError(message)
    if (remoteSendActiveRef.current) {
      useChatInputStore.getState().reportRemoteSendRefusal(message)
    }
  }, [])

  /** 发送消息。正文读 textRef（与 text state 逐点同步，见各 setText 写点）：
   *  远程发送（全屏浮窗）经 store 订阅调用本函数时渲染闭包可能滞后一拍，
   *  ref 读取保证取到浮窗刚写入的文本 */
  const handleSend = useCallback(() => {
    const trimmedText = textRef.current.trim()
    // 本输入器已入列引用条目（R5 卡片通道）：发送装配期按序拼块引用进正文尾部
    const draftCitations = draftKey
      ? useChatInputStore.getState().citationsByDraftKey[draftKey]
      : undefined

    const hasContent = trimmedText.length > 0 || (draftCitations?.length ?? 0) > 0
    const hasAttachments = attachments.length > 0
    const hasPendingFiles = pendingFiles.some((pf) => pf.status === 'success')

    if (!hasContent && !hasAttachments && !hasPendingFiles) return
    // 发送态（本管道在途未落定）不可再次发送：输入锁定后 UI 不可达（textarea
    // disabled 不产生键盘事件、按钮转圈禁用），此处兜底防御
    if (sendInFlight) return
    // 禁用态发送必须明示拒绝（真机缺陷"busy 会话发送静默抑制"唯一静默点：
    // 文本滞留输入框+零反馈零网络）；空输入静默返回属未尝试发送，不在此列
    if (disabled) {
      reportSendRefusal('会话执行中，暂不能发送——请切回会话主标签后再试')
      return
    }

    // 在途附件不随消息发出（[来源: docs/working/packtest/R236_多模态断链BUG62_20260922.md]）：
    // 附件引用在发送瞬间才并入正文（appendAttachmentRefs 只认已有 /uploads/ url 的
    // 附件），在途附件拿不到引用，照发即 LLM 收不到该附件（模型看到「看这张图」
    // 却无图可看）。显式拒绝并提示（拒绝必须反馈），正文与附件原样保留，
    // 上传完成后重发即可。
    const hasUploadingAttachment = pendingFiles.some(
      (pf) => pf.status === 'pending' || pf.status === 'uploading',
    )
    if (hasUploadingAttachment) {
      reportSendRefusal('附件正在上传，请等待上传完成后再发送')
      return
    }

    /** 待发引用（卡片条目+provider 实时选中）并入正文、上传完成附件并入附件面：
     *  装配见 composerSend */
    const params = composeSendParams({
      trimmedText,
      attachments,
      pendingFiles,
      thinkingStrength: currentThinkingStrength,
      modelId: currentModelId,
      composerText: textRef.current,
      draftKey,
      citations: draftCitations,
    })

    // 受理协议：false = 未受理（无会话/令牌、管道未就绪、子标签不支持等），保留
    // 输入、附件、草稿与引用供用户重试；true/void（兼容既有只发不回的回调）=
    // 受理，进入发送态。
    const accepted = onSendMessage(params)
    if (accepted === false) {
      // 未受理必须在操作点显式反馈（OBS-25）：拒绝详情由各拒绝层写通知中心
      // （入口在侧栏，输入时不一定可见），输入区不落提示的话用户视角仍是
      // 「点了没反应」。拒发不触碰任何流式状态，「停止生成」只由真实
      // isGenerating 驱动。
      reportSendRefusal('消息未发送，请检查会话状态')
      return
    }

    // 受理 = 发送态开始（2026-11-04 落定语义重定义）：文本/附件/草稿保留到落定
    // （气泡认领上屏/队列条出现）那一刻——清空在本组件的发送终局订阅回调按
    // 快照比对执行；失败（ack 拒收/20s 看门狗）文本原位保留天然可重试。
    for (const provider of getReferenceProviders()) {
      void provider.consume?.()
    }
    setInputError(null)
  }, [attachments, pendingFiles, disabled, sendInFlight, onSendMessage, currentThinkingStrength, currentModelId, draftKey, reportSendRefusal])

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

      if (imageFiles.length === 0) return
      e.preventDefault()

      // 模型支持图片 → 附件管线（既有路径）
      if (inputCapabilities.canPasteImage) {
        const dataTransfer = new DataTransfer()
        imageFiles.forEach((file) => dataTransfer.items.add(file))
        handleFileSelect(dataTransfer.files)
        return
      }

      // 纯文本模型降级（paste-to-path）：上传图片后把 markdown 图片引用插入
      // 输入框——消息气泡渲染成图，multimodal_preprocessor 按 /uploads/ 引用
      // 提取内容块，llm_core 发送前转 base64。与附件管线共享 uploadFile。
      void (async () => {
        const inserts: string[] = []
        for (const file of imageFiles) {
          try {
            const result = await uploadFile(file, modelName)
            const name = file.name.replace(/[[\]]/g, '').trim() || '图片'
            inserts.push(`![${name}](${result.url})`)
          } catch {
            inserts.push(`图片上传失败：${file.name}`)
          }
        }
        if (inserts.length === 0) return
        const currentText = textRef.current
        const newText = currentText ? `${currentText}\n${inserts.join('\n')}` : inserts.join('\n')
        setText(newText)
        textRef.current = newText
        if (draftKey) {
          useChatInputStore.getState().saveDraft(draftKey, newText)
        }
        setTimeout(() => {
          adjustTextareaHeight()
          textareaRef.current?.focus()
        }, 0)
      })()
    },
    [inputCapabilities.canPasteImage, handleFileSelect, modelName, draftKey, adjustTextareaHeight],
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

  /** 全屏浮窗镜像同步（2026-11-05 阶段一件一）：草稿是双向共享真源，本组件
   *  每次编辑实时 saveDraft（组件→store），浮窗编辑经 saveDraft 写入的变更由
   *  本订阅同步回本地文本（store→组件），构成同源同内容。守卫：草稿与
   *  textRef 相同值跳过（自家 saveDraft 回声 / 挂载初值），无重渲染环。 */
  useEffect(() => {
    if (!draftKey) return
    const syncFromDraft = (state: { drafts: Record<string, string> }) => {
      const draft = state.drafts[draftKey]
      if (draft === undefined || draft === textRef.current) return
      textRef.current = draft
      setText(draft)
      setTimeout(adjustTextareaHeight, 0)
    }
    syncFromDraft(useChatInputStore.getState())
    return useChatInputStore.subscribe(syncFromDraft)
  }, [draftKey, adjustTextareaHeight])

  /** 远程发送（全屏浮窗）消费：浮窗只触发 requestRemoteSend，本组件订阅消费并
   *  以自身完整状态调用 handleSend——与主输入框完全相同的发送路径与参数装配
   *  （附件/模型等局部状态仅真实组件可达，浮窗不拼参）。挂载即消费一次。 */
  const handleSendRef = useRef(handleSend)
  useEffect(() => {
    handleSendRef.current = handleSend
  }, [handleSend])
  useEffect(() => {
    const tryRemoteSend = () => {
      if (!useChatInputStore.getState().pendingRemoteSend) return
      useChatInputStore.getState().consumeRemoteSend()
      remoteSendActiveRef.current = true
      try {
        handleSendRef.current()
      } finally {
        remoteSendActiveRef.current = false
      }
    }
    tryRemoteSend()
    return useChatInputStore.subscribe(tryRemoteSend)
  }, [])

  /** 清空输入面（落定成功路径专用）：文本/附件/在途附件/草稿/引用条目一次性清，
   *  恢复受理前姿态（高度/折叠态/警告条） */
  const clearComposer = useCallback(() => {
    setText('')
    textRef.current = ''
    interimVoiceStartRef.current = -1
    setAttachments([])
    setPendingFiles([])
    if (draftKey) {
      useChatInputStore.getState().clearDraft(draftKey)
      useChatInputStore.getState().clearCitations(draftKey)
    }
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
    }
    setIsExpanded(false)
  }, [draftKey])

  /** 发送终局消费（落定/判死）：store outcome 按 id 增量消费，接缝见 hook */
  useSendOutcomeConsumer({ draftKey, textRef, onLanded: clearComposer, onError: setInputError })

  const isUploading = pendingFiles.some((pf) => pf.status === 'uploading')
  const isCompactMode = mode === 'compact'
  // 触屏无物理 Enter/Shift 键，桌面键盘操作提示在移动端是无效信息且
  // 393px 视口下必然折两行撑高输入区——移动端占位只保留输入语义
  const isMobile = useIsMobileViewport()

  /** 本输入器已入列引用条目数（R5 卡片通道，发送按钮可用性与发送许可计入——
   *  旧通道引用文本即正文可发，卡片化后仅条目无正文保持同等能力） */
  const draftCitationsCount = useChatInputStore((s) =>
    draftKey ? (s.citationsByDraftKey[draftKey]?.length ?? 0) : 0,
  )

  const hasInputContent =
    text.trim().length > 0 ||
    attachments.length > 0 ||
    draftCitationsCount > 0 ||
    pendingFiles.some((pf) => pf.status === 'success')

  const canSend = hasInputContent && !disabled && !isUploading

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
      {/* 输入区错误提示（上传失败/发送未受理/发送判死） */}
      {inputError && (
        <div
          id="input-error"
          role="alert"
          className="text-destructive bg-destructive/10 mb-3 flex items-center gap-2 rounded-xl p-2 text-sm"
        >
          <AlertCircle className="h-icon-md w-icon-md flex-shrink-0" />
          <span className="flex-1">{inputError}</span>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 w-6 rounded-lg p-0"
            onClick={() => setInputError(null)}
            aria-label="关闭错误提示"
          >
            <X className="h-icon-md w-icon-md" />
          </Button>
        </div>
      )}

      {/* 输入框容器：输入区域与下方图标工具栏一体，不显示分割边框 */}
      <div
        data-testid="chat-input-box"
        className={cn(
          'relative rounded-xl transition-shadow duration-200',
          'bg-[var(--hover-overlay)]',
          // 皮肤输入面（翻译器按皮肤 patches.css 提取 --chat-input-bg——
          // 对话框颜色跟皮肤一样；无声明回退既有 hover-overlay）
          'bg-[var(--chat-input-bg,var(--hover-overlay))]',
        )}
      >
        {/* 已入列引用卡片行（R5 卡片化/R6 容器内顶部）：挂在输入框容器本体内部
            （邮件客户端收件人 chips 形态）；pr-10 让出右上角展开/收起悬浮钮位
            （与 textarea 同款预留，保证卡片 × 可点）。ReferenceSelectionRow
            （实时选中镜像）仍在容器外上方——已定格条目与实时选中是两条通道，
            共存不合并 */}
        <CitationCardRow draftKey={draftKey} className="pr-10" />

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
          {isExpanded ? <Minimize2 className="h-icon-md w-icon-md" /> : <Maximize2 className="h-icon-md w-icon-md" />}
        </Button>

        {/* 权限模式等输入区选择器由声明驱动渲染于底部工具栏
            （chat-input 空间附加式 DeclaredWidgetLayer），见下方工具栏 */}

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

        {/* 录音状态条（容器内顶部，与附件预览区结构对称） */}
        <ComposerRecordingBanner
          isRecording={voiceInput.isRecording}
          mode={voiceInput.mode}
          seconds={voiceInput.recordingDuration}
        />

        {/* 文本输入框 */}
        <textarea
          ref={textareaRef}
          value={text}
          onChange={handleTextChange}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={composerPlaceholder({
            sendInFlight,
            isExecuting,
            mode,
            isDragging,
            canDragDrop: enableDragDrop && inputCapabilities.canDragDrop,
            isMobile,
            canAttach: enableFileUpload && !isCompactMode && inputCapabilities.showAttachmentButton,
          })}
          disabled={disabled || sendInFlight}
          rows={1}
          data-testid="chat-input-textarea"
          aria-label="消息输入"
          aria-describedby={inputError ? 'input-error' : undefined}
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

        {/* 底部工具栏：左组可收缩可换行（min-w-0 flex-1 flex-wrap），发送按钮
            shrink-0——大字体主题/插件动作多/窄栏（右面板展开压缩中栏）时左组
            按钮换行排布而非叠压溢出，发送按钮不被挤出容器 */}
        <div className="flex min-w-0 items-center justify-between gap-2 px-3 pb-3">
          <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
            {/* 附件按钮 */}
            {enableFileUpload && !isCompactMode && inputCapabilities.showAttachmentButton && (
              <Button
                variant="ghost"
                size="icon"
                className="text-muted-foreground hover:text-foreground hover:bg-muted h-11 w-11 shrink-0 rounded-lg sm:h-8 sm:w-8"
                onClick={triggerFileSelect}
                disabled={disabled || isExecuting}
                title="添加附件"
                aria-label="添加附件"
              >
                <Paperclip className="h-icon-md w-icon-md" />
              </Button>
            )}

            {/* 语音输入槽位（chat-input 空间）：前端默认 VoiceInputButton，
                插件声明 id=voice_input 可覆盖（换 webview 即完全接管） */}
            {!isCompactMode && voiceInput.isSupported && (
              <DeclaredWidgetLayer
                space="chat-input"
                slotId="voice_input"
                fallback={VoiceInputButton}
                fallbackProps={{
                  disabled: disabled || isExecuting,
                  state: voiceInput.state,
                  error: voiceInput.error,
                  recordingDuration: voiceInput.recordingDuration,
                  'aria-label': voiceInput.isRecording ? '停止语音输入' : '开始语音输入',
                  onClick: () => {
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
                  },
                }}
                className="flex-row items-center min-w-0"
              />
            )}

        {/* 附身指示条 chip（mode.possess 桥）：附身档由本组件订阅传入，
            未附身零渲染；解除与展示契约见 ChatInputPossessChip */}
        <ChatInputPossessChip possessed={possessed} />

        {/* 会话身份绑定指示条（mode.session 桥落地的会话执行选项绑定）：绑定
            存在且未附身时常驻展示 + 一键退出；与附身 chip 互斥（附身是显式
            全局态，优先展示——发送链同序：附身独占时会话绑定全让位） */}
        {!possessed && sessionAgent && (
          <SessionAgentIndicator name={sessionAgent.name} onExit={handleExitSessionBinding} />
        )}

        {/* 插件声明式工具栏 widget（chat-input 空间附加式）：权限模式选择器
            （插件 ui_schema 声明驱动，跟随当前选中管道标签）。
            excludeIds 排除已被上方槽位层消费的声明（voice_input/context_usage/
            thinking_strength）防重复渲染；task_mode 一并排除（R10③ 收拢裁定
            2026-10-14：任务/助理模式是会话级选择（出生即定），不经输入行选——
            模式会话从侧栏模式面板等选择处出生；声明过滤见
            taskModeOptionsFromModes 的 foreground_selectable 通道） */}
        <DeclaredWidgetLayer
          space="chat-input"
          className="flex-row items-center min-w-0"
          excludeIds={['voice_input', 'context_usage', 'thinking_strength', 'task_mode', 'model_id']}
        />
            {/* 思考选择槽位（chat-input 空间，数据归属 llm_core）：渲染 llm_core
                声明的 form（select），值/回调由宿主注入；选项由声明 fieldsUri
                数据源下发（当前模型 thinking_strength_params 参数组，配置空时
                声明层整体不渲染） */}
            {enableThinkingMode && !isCompactMode && (
              <DeclaredWidgetLayer
                space="chat-input"
                slotId="thinking_strength"
                overrideProps={thinkingStrengthBridge}
                contextVars={{ model: modelName ?? '' }}
                className="flex-row items-center min-w-0"
              />
            )}

            {/* 模型选择槽位（chat-input 空间，数据归属 llm_core）：渲染 llm_core
                声明的 model_select（frame_field=model_id 传输声明）；值/回调由宿主
                注入，显式选择随消息 model_id 字段透传（最高优先级覆盖管道配置），
                '' = 未选择（占位显示配置模型名，帧不带字段） */}
            <DeclaredWidgetLayer
              space="chat-input"
              slotId="model_id"
              overrideProps={modelIdBridge}
              className="flex-row items-center min-w-0"
            />

            {/* token 用量槽位（chat-input 空间）：默认件 = 插件声明同款
                ContextUsageWidget（数据从管道 state 读：llm_model /
                context_window / track.llm_usage，query 缓存共享 + 事件失效化
                实时刷新），插件声明 id=context_usage 可覆盖声明 props；模型名
                展示归 model_id 选择器（拆分裁定：圈圈=纯上下文信息） */}
            <DeclaredWidgetLayer
              space="chat-input"
              slotId="context_usage"
              fallback={ContextUsageWidget}
              fallbackProps={{ modelName, ringOnly: true }}
              className="flex-row items-center min-w-0"
            />

            {/* 插件声明的聊天输入动作（chat 空间 input-action 声明驱动） */}
            <ChatInputActions />
          </div>

          <ComposerSendButton
            sendInFlight={sendInFlight}
            isExecuting={isExecuting}
            hasInputContent={hasInputContent}
            canSend={canSend}
            onStopGenerate={onStopGenerate}
            onSend={handleSend}
          />
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
