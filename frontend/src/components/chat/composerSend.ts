/** 输入器发送面纯函数（ChatInput 破千行冻结拆件，2026-11-04）：
 *  输入框占位文案与发送参数装配。ChatInput 只保留状态编排与渲染。
 *  设计真值 = 发送受理协议 + 落定清空快照比对（ADR/台账口径见 ChatInput 内注释）。 */

import { buildReferenceBlock, getReferenceProviders, type ReferenceSelection } from '@/services/references'
import { composeCitationEntry, type CitationEntryInput } from '@/utils/citationCompose'
import type { Attachment, ChatInputMode, PendingFile, SendMessageParams } from './types'
import type { CitationItem } from '@/stores/chatInputStore'
import type { ThinkingStrength } from '@/types/thinkingMode'

/** 占位文案判定入参（逐项对应渲染期的状态/能力开关） */
export interface ComposerPlaceholderState {
  /** 发送态（已受理未落定，输入锁定） */
  sendInFlight: boolean
  /** 执行中（智能模式看管道运行态，其余模式看生成态） */
  isExecuting: boolean
  /** 输入器形态（smart 档执行中文案不同） */
  mode?: ChatInputMode
  /** 拖拽悬停中 */
  isDragging: boolean
  /** 拖拽上传可用（外部开关 ∧ 模型能力） */
  canDragDrop: boolean
  /** 移动端视口 */
  isMobile: boolean
  /** 附件按钮可用（外部开关 ∧ 非紧凑态 ∧ 模型能力） */
  canAttach: boolean
}

/** 输入框占位文案：发送态 > 执行态 > 拖拽态 > 移动端 > 桌面端输入提示 */
export function composerPlaceholder(state: ComposerPlaceholderState): string {
  // 发送态文案在文本框有内容时不可见，附件-only 发送等空文本场景可见
  if (state.sendInFlight) return '发送中…'
  if (state.isExecuting) return state.mode === 'smart' ? '执行中...' : '正在生成回复...'
  if (state.isDragging && state.canDragDrop) return '松开鼠标上传文件'
  if (state.isMobile) return '输入消息'
  if (state.canAttach) return 'Enter 发送 · Shift+Enter 换行 · 支持拖拽上传'
  return 'Enter 发送 · Shift+Enter 换行'
}

/** 发送参数装配入参 */
export interface ComposeSendParamsInput {
  /** 已 trim 的正文（待发引用块在函数内拼接） */
  trimmedText: string
  /** 已就绪附件（正文引用的既有附件） */
  attachments: Attachment[]
  /** 在途/待发附件（只并入上传完成的） */
  pendingFiles: PendingFile[]
  /** 思考强度（参数组 JSON 串，'' = 不覆盖） */
  thinkingStrength: ThinkingStrength
  /** 显式模型选择（'' = 不带字段，跟随配置） */
  modelId: string
  /** 受理快照正文（落定清空比对用） */
  composerText: string
  /** 草稿键（受理快照的稳定锚） */
  draftKey?: string
  /** 已入列引用条目（R5 卡片通道：按序拼回 markdown 块引用，位置=正文尾部，
   *  与旧 requestInsert 追加等价；缺省/空列行为零变化） */
  citations?: CitationItem[]
}

/**
 * CitationItem → markdown 组装入参：sourceLabel=来源注记（title），content=定格
 * 快照（excerpt）；消息类条目以 createdAt 还原来源时间注记（旧通道「角色 · 时间」
 * 形态），文件/制品类不带时间。
 */
function citationItemToEntry(item: CitationItem): CitationEntryInput {
  return {
    content: item.excerpt,
    sourceLabel: item.title,
    ...(item.kind === 'message'
      ? { timestamp: new Date(item.createdAt).toISOString() }
      : {}),
    ...(item.comment ? { comment: item.comment } : {}),
  }
}

/** 装配 SendMessageParams：正文并入待发引用块，附件并入上传完成件 */
export function composeSendParams(input: ComposeSendParamsInput): SendMessageParams {
  const pendingRefs = getReferenceProviders()
    .map((p) => p.getSelection())
    .filter((sel): sel is ReferenceSelection => !!sel && sel.items.length > 0)
  let content = input.trimmedText
  // R5 卡片通道：入列引用条目按序拼回 markdown 块引用（组装真源=citationCompose，
  // 与旧 requestInsert 插入的字符串逐字等价），位置=正文尾部；正文空时条目即
  // 开头（旧通道等价：引用文本单独成稿），否则空行分隔（旧通道为单 \n 续接，
  // markdown 段落语义等价）。卡片块在 provider 实时引用块之前（入列条目属正文
  // 尾部，实时引用保持队尾）。
  for (const item of input.citations ?? []) {
    const block = composeCitationEntry(citationItemToEntry(item))
    content = content ? content + '\n\n' + block : block
  }
  for (const ref of pendingRefs) {
    const block = buildReferenceBlock(ref)
    if (block) content = content + '\n\n' + block
  }

  const allAttachments: Attachment[] = [...input.attachments]
  for (const pf of input.pendingFiles) {
    if (pf.status !== 'success' || !pf.uploadResult) continue
    allAttachments.push({
      id: pf.id,
      name: pf.uploadResult.filename,
      type: pf.uploadResult.mime_type,
      size: pf.file.size,
      url: pf.uploadResult.url,
      status: 'completed',
    })
  }

  return {
    content,
    attachments: allAttachments.length > 0 ? allAttachments : undefined,
    // thinkingStrength = 选中的参数组 JSON 串（'' = 未选择，后端不覆盖）
    thinkingStrength: input.thinkingStrength,
    // modelId = 显式模型选择（'' = 未选择，帧不带 model_id 字段，跟随配置）
    modelId: input.modelId || undefined,
    // composerText/draftKey = 落定清空契约（受理时经 beginSend 登记）：清空
    // 只发生在落定订阅回调，按「当前文本 == 受理时快照」比对执行
    composerText: input.composerText,
    draftKey: input.draftKey,
  }
}
