/** @feature: FP-T12 前端适配（发送态生命周期：占位文案 + 发送参数装配） | @ci: frontend-test */
/**
 * composerSend 纯函数测试（ChatInput 拆件）：
 * - composerPlaceholder：发送态 > 执行态（smart/非 smart 双文案）> 拖拽态 >
 *   移动端 > 附件可用 > 纯键盘提示，逐档断言；
 * - composeSendParams：引用块并入正文（空 items 不产出）、上传完成附件并入
 *   附件面（未完成/无结果不并入）、空附件面落 undefined、modelId '' 不带字段、
 *   composerText/draftKey 快照原样透传。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  composeSendParams,
  composerPlaceholder,
  type ComposerPlaceholderState,
} from '@/components/chat/composerSend'
import { registerReferenceProvider, type ReferenceSelection } from '@/services/references'
import { composeCitationEntry } from '@/utils/citationCompose'
import type { Attachment, PendingFile } from '@/components/chat/types'
import type { CitationItem } from '@/stores/chatInputStore'

const baseState: ComposerPlaceholderState = {
  sendInFlight: false,
  isExecuting: false,
  mode: 'full',
  isDragging: false,
  canDragDrop: true,
  isMobile: false,
  canAttach: true,
}

describe('composerPlaceholder（占位文案分档）', () => {
  it('发送态优先于一切（执行中/拖拽都让位）', () => {
    expect(composerPlaceholder({ ...baseState, sendInFlight: true })).toBe('发送中…')
    expect(
      composerPlaceholder({ ...baseState, sendInFlight: true, isExecuting: true, mode: 'smart' }),
    ).toBe('发送中…')
  })

  it('执行态按模式分流：smart=执行中，其余=正在生成回复', () => {
    expect(composerPlaceholder({ ...baseState, isExecuting: true, mode: 'smart' })).toBe('执行中...')
    expect(composerPlaceholder({ ...baseState, isExecuting: true, mode: 'full' })).toBe(
      '正在生成回复...',
    )
    expect(composerPlaceholder({ ...baseState, isExecuting: true, mode: undefined })).toBe(
      '正在生成回复...',
    )
  })

  it('拖拽态只在拖拽上传可用时生效（能力关闭回落下一档）', () => {
    expect(composerPlaceholder({ ...baseState, isDragging: true })).toBe('松开鼠标上传文件')
    expect(composerPlaceholder({ ...baseState, isDragging: true, canDragDrop: false })).toBe(
      'Enter 发送 · Shift+Enter 换行 · 支持拖拽上传',
    )
    expect(
      composerPlaceholder({ ...baseState, isDragging: true, canDragDrop: false, canAttach: false }),
    ).toBe('Enter 发送 · Shift+Enter 换行')
  })

  it('移动端提示优先于附件提示，且带按住说话引导（语音输入可发现性）', () => {
    expect(composerPlaceholder({ ...baseState, isMobile: true })).toBe('输入消息，可按住麦克风说话')
    expect(composerPlaceholder({ ...baseState, isMobile: true, canAttach: false })).toBe(
      '输入消息，可按住麦克风说话',
    )
  })

  it('桌面端按附件能力给提示', () => {
    expect(composerPlaceholder({ ...baseState, canAttach: true })).toBe(
      'Enter 发送 · Shift+Enter 换行 · 支持拖拽上传',
    )
    expect(composerPlaceholder({ ...baseState, canAttach: false })).toBe(
      'Enter 发送 · Shift+Enter 换行',
    )
  })
})

const makeAttachment = (id: string): Attachment => ({
  id,
  name: `${id}.png`,
  type: 'image/png',
  size: 10,
  url: `/uploads/${id}.png`,
  status: 'completed',
})

const makePendingFile = (id: string, over: Partial<PendingFile> = {}): PendingFile => ({
  id,
  file: new File(['x'], `${id}.txt`, { type: 'text/plain' }),
  status: 'pending',
  ...over,
})

describe('composeSendParams（发送参数装配）', () => {
  const selection = vi.fn<() => ReferenceSelection | null>(() => null)

  afterEach(() => {
    selection.mockReset()
    selection.mockReturnValue(null)
  })

  it('无引用无附件：正文原样、attachments/modelId 落 undefined、快照透传', () => {
    const params = composeSendParams({
      trimmedText: '你好',
      attachments: [],
      pendingFiles: [],
      thinkingStrength: '{"level":"high"}',
      modelId: '',
      composerText: '你好 ',
      draftKey: 'draft-1',
    })
    expect(params.content).toBe('你好')
    expect(params.attachments).toBeUndefined()
    expect(params.modelId).toBeUndefined()
    expect(params.thinkingStrength).toBe('{"level":"high"}')
    expect(params.composerText).toBe('你好 ')
    expect(params.draftKey).toBe('draft-1')
  })

  it('待发引用并入正文（provider 有选中项才拼块，空 items 不产出）', () => {
    registerReferenceProvider({ source: 'test-src', getSelection: () => selection() })
    selection.mockReturnValue({
      source: 'test-src',
      items: [{ name: '笔记', type: 'file', path: '/tmp/a.md' }],
    })
    const params = composeSendParams({
      trimmedText: '看这个',
      attachments: [],
      pendingFiles: [],
      thinkingStrength: '',
      modelId: 'deepseek-v3',
      composerText: '看这个',
    })
    expect(params.content).toContain('看这个')
    expect(params.content).toContain('<reference source="test-src">')
    expect(params.content).toContain('- 笔记 (file) @ /tmp/a.md')

    selection.mockReturnValue({ source: 'test-src', items: [] })
    const empty = composeSendParams({
      trimmedText: '看这个',
      attachments: [],
      pendingFiles: [],
      thinkingStrength: '',
      modelId: '',
      composerText: '看这个',
    })
    expect(empty.content).toBe('看这个')
  })

  it('附件面：既有附件保留 + 只并入上传完成的在途附件', () => {
    const params = composeSendParams({
      trimmedText: '带附件',
      attachments: [makeAttachment('a1')],
      pendingFiles: [
        makePendingFile('p-ok', {
          status: 'success',
          uploadResult: {
            file_id: 'p-ok',
            filename: 'ok.txt',
            mime_type: 'text/plain',
            media_type: 'document',
            size: 3,
            url: '/uploads/ok.txt',
          },
        }),
        makePendingFile('p-uploading', { status: 'uploading' }),
        makePendingFile('p-noresult', { status: 'success' }),
        makePendingFile('p-error', { status: 'error', error: '上传失败' }),
      ],
      thinkingStrength: '',
      modelId: 'gpt-x',
      composerText: '带附件',
    })
    expect(params.attachments).toHaveLength(2)
    expect(params.attachments?.map((a) => a.id)).toEqual(['a1', 'p-ok'])
    expect(params.attachments?.[1]).toMatchObject({
      name: 'ok.txt',
      type: 'text/plain',
      // size 取本地文件字节数（uploadResult 的 size 不参与附件面装配）
      size: 1,
      url: '/uploads/ok.txt',
      status: 'completed',
    })
    expect(params.modelId).toBe('gpt-x')
  })
})

describe('composeSendParams 引用条目装配（R5 卡片通道：发送产物不变）', () => {
  const CREATED_AT = Date.parse('2026-11-05T06:30:00Z')

  /** 本 describe 专用 provider 桩（source 独立防串邻 describe；afterEach 归零） */
  const refSelection = vi.fn<() => ReferenceSelection | null>(() => null)
  beforeEach(() => {
    registerReferenceProvider({
      source: 'test-r5',
      getSelection: () => refSelection(),
    })
  })
  afterEach(() => {
    refSelection.mockReset()
    refSelection.mockReturnValue(null)
  })

  const msgItem: CitationItem = {
    id: 'c1',
    kind: 'message',
    title: '用户',
    excerpt: '评审这一段',
    createdAt: CREATED_AT,
  }
  const codeItem: CitationItem = {
    id: 'c2',
    kind: 'code',
    title: 'src/main.py:L2',
    excerpt: 'L2: line2',
    comment: '复核',
    createdAt: CREATED_AT,
  }

  const baseInput = {
    attachments: [],
    pendingFiles: [],
    thinkingStrength: '',
    modelId: '',
    composerText: '看这个',
    draftKey: 'k',
  }

  it('卡片条目按序拼回 markdown 块引用，条目块与旧通道 requestInsert 输出逐字等价', () => {
    const params = composeSendParams({
      ...baseInput,
      trimmedText: '看这个',
      citations: [msgItem, codeItem],
    })
    // 旧通道等价真源：同一 item 内容经 citationCompose 组装（旧 requestInsert
    // 插入的正是这些字符串）；消息条目以 createdAt 还原来源时间注记
    const oldChannel1 = composeCitationEntry({
      content: '评审这一段',
      sourceLabel: '用户',
      timestamp: new Date(CREATED_AT).toISOString(),
    })
    const oldChannel2 = composeCitationEntry({
      content: 'L2: line2',
      sourceLabel: 'src/main.py:L2',
      comment: '复核',
    })
    expect(params.content).toBe(`看这个\n\n${oldChannel1}\n\n${oldChannel2}`)
    // 字面锚：块引用形态与评语段在位（防纯同源函数比对空转）
    expect(params.content).toContain('> 【用户 · ')
    expect(params.content).toContain('> 评审这一段')
    expect(params.content).toContain('> 【src/main.py:L2】\n> L2: line2\n\n复核')
  })

  it('code 条目不带时间注记；消息条目时间注记来自 createdAt', () => {
    const params = composeSendParams({
      ...baseInput,
      trimmedText: '',
      citations: [codeItem, msgItem],
    })
    expect(params.content).toContain('> 【src/main.py:L2】\n')
    expect(params.content).not.toContain('【src/main.py:L2 · ')
    expect(params.content).toContain('> 【用户 · ')
  })

  it('卡片块先于 provider 实时引用块（入列条目=正文尾部，实时引用随队尾不变）', () => {
    refSelection.mockReturnValue({
      source: 'test-r5',
      items: [{ name: '笔记', type: 'file', path: '/tmp/a.md' }],
    })
    const params = composeSendParams({
      ...baseInput,
      trimmedText: '正文',
      citations: [codeItem],
    })
    const citationBlock = composeCitationEntry({
      content: 'L2: line2',
      sourceLabel: 'src/main.py:L2',
      comment: '复核',
    })
    expect(params.content).toBe(
      `正文\n\n${citationBlock}\n\n<reference source="test-r5">\n- 笔记 (file) @ /tmp/a.md\n</reference>`,
    )
  })

  it('citations 缺省/空列 → 行为零变化（既有通道回归面）', () => {
    const noParam = composeSendParams({ ...baseInput, trimmedText: '原样' })
    const empty = composeSendParams({ ...baseInput, trimmedText: '原样', citations: [] })
    expect(noParam.content).toBe('原样')
    expect(empty.content).toBe('原样')
  })
})
