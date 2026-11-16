/** @feature FP-T12 前端适配 | @ci: frontend-test */
// @ci: frontend-test
/**
 * systemNotifier 系统通知决策层测试
 *
 * 覆盖：决策规则 v3（notify_worthy 标记 = 唯一弹出依据 / 无标记一律静默，
 * 可见性兜底已撤销）、正文截断与空白折叠、文本提取、dedupeKey 幂等（同 id
 * 只决策一次）、桥接失败折算 false 不上抛。桥接行为本身由
 * utils/__tests__/systemNotification.test.ts 覆盖。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  SYSTEM_NOTIFY_BODY_MAX_CHARS,
  extractNotifyText,
  notifyPipelineMessage,
  resetSystemNotifierForTest,
  shouldNotifyOnMessage,
  truncateNotifyBody,
} from '@/lib/systemNotifier'

type NotificationBridge = {
  show: (opts: { title: string; body: string }) => Promise<boolean>
}

function setBridge(bridge: NotificationBridge | undefined): void {
  if (bridge === undefined) {
    delete (window as { electronAPI?: unknown }).electronAPI
    return
  }
  ;(window as { electronAPI?: unknown }).electronAPI = { notification: bridge }
}

/** 设定文档可见性/焦点（证明决策与其无关：有标记必弹、无标记必静默） */
function setDocumentState(visibilityState: 'visible' | 'hidden', hasFocus: boolean): void {
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue(visibilityState)
  vi.spyOn(document, 'hasFocus').mockReturnValue(hasFocus)
}

describe('shouldNotifyOnMessage 决策规则（v3 判定器驱动）', () => {
  it('notify_worthy 标记：弹（唯一弹出依据）', () => {
    expect(shouldNotifyOnMessage({ notifyWorthy: true })).toBe(true)
  })

  it('无标记（缺省）：静默不弹', () => {
    expect(shouldNotifyOnMessage({})).toBe(false)
  })

  it('notify_worthy 显式 false：静默不弹', () => {
    expect(shouldNotifyOnMessage({ notifyWorthy: false })).toBe(false)
  })

  it('非 boolean true 值（字符串 "true"）：视作无标记，静默不弹（严格 === true 契约）', () => {
    expect(shouldNotifyOnMessage({ notifyWorthy: 'true' as unknown as boolean })).toBe(false)
  })
})

describe('truncateNotifyBody / extractNotifyText', () => {
  it('短文本原样保留', () => {
    expect(truncateNotifyBody('提醒：下午三点开会')).toBe('提醒：下午三点开会')
  })

  it('超长截断到 80 字（性质：结果长度恒 ≤ 上限）', () => {
    const long = '一'.repeat(200)
    const out = truncateNotifyBody(long)
    expect(out.length).toBe(SYSTEM_NOTIFY_BODY_MAX_CHARS)
    expect(out.length).toBeLessThanOrEqual(SYSTEM_NOTIFY_BODY_MAX_CHARS)
  })

  it('换行与连续空白折叠为单空格', () => {
    expect(truncateNotifyBody('第一行\n\n第二行\t制表')).toBe('第一行 第二行 制表')
  })

  it('extractNotifyText：顶层 content 优先', () => {
    expect(
      extractNotifyText({
        content: '顶层文本',
        parts: [{ type: 'text', content: 'parts文本' }],
      }),
    ).toBe('顶层文本')
  })

  it('extractNotifyText：content 为空时回落 text parts 拼接（跳过非 text part）', () => {
    expect(
      extractNotifyText({
        content: '',
        parts: [
          { type: 'thinking', content: '思考' },
          { type: 'text', content: '正文一' },
          { type: 'tool_call' },
          { type: 'text', content: '正文二' },
        ],
      }),
    ).toBe('正文一正文二')
  })
})

describe('notifyPipelineMessage 出口行为', () => {
  beforeEach(() => {
    resetSystemNotifierForTest()
    localStorage.removeItem('notification_sound_muted')
    vi.restoreAllMocks()
  })

  it('notifyWorthy 标记 + 前台正在看：弹（title/body 原样到桥接）', async () => {
    setDocumentState('visible', true)
    const show = vi.fn(async () => true)
    setBridge({ show })

    await expect(
      notifyPipelineMessage({ notifyWorthy: true, title: '灵汐', body: '会提醒正文', dedupeKey: 'm1' }),
    ).resolves.toBe(true)
    expect(show).toHaveBeenCalledTimes(1)
    expect(show).toHaveBeenCalledWith({ title: '灵汐', body: '会提醒正文' })
  })

  it('携带 threadId → 原样透传到桥接（通知点击跳转对应对话的载荷）', async () => {
    setDocumentState('hidden', true)
    const show = vi.fn(async () => true)
    setBridge({ show })

    await expect(
      notifyPipelineMessage({
        notifyWorthy: true,
        title: '灵汐',
        body: '带会话坐标的正文',
        threadId: 'th-nav-1',
        dedupeKey: 'm7',
      }),
    ).resolves.toBe(true)
    expect(show).toHaveBeenCalledWith({
      title: '灵汐',
      body: '带会话坐标的正文',
      threadId: 'th-nav-1',
    })
  })

  it('notifyWorthy 标记 + 后台：弹；同 dedupeKey 重放只弹一次（dedupe 先于必弹规则）', async () => {
    setDocumentState('hidden', true)
    const show = vi.fn(async () => true)
    setBridge({ show })

    await expect(
      notifyPipelineMessage({ notifyWorthy: true, title: '灵汐', body: '后台必弹', dedupeKey: 'm3' }),
    ).resolves.toBe(true)
    expect(show).toHaveBeenCalledTimes(1)
    await expect(
      notifyPipelineMessage({ notifyWorthy: true, title: '灵汐', body: '后台必弹', dedupeKey: 'm3' }),
    ).resolves.toBe(false)
    expect(show).toHaveBeenCalledTimes(1)
  })

  it('无标记 + 文档隐藏：不弹（v3 撤销可见性兜底——防 away 时后台消息轰炸）', async () => {
    setDocumentState('hidden', true)
    const show = vi.fn(async () => true)
    setBridge({ show })

    await expect(
      notifyPipelineMessage({ title: '灵汐', body: '后台碎语', dedupeKey: 'm2' }),
    ).resolves.toBe(false)
    expect(show).not.toHaveBeenCalled()
  })

  it('无标记 + 前台正在看：不弹', async () => {
    setDocumentState('visible', true)
    const show = vi.fn(async () => true)
    setBridge({ show })

    await expect(
      notifyPipelineMessage({ title: '灵汐', body: '正文', dedupeKey: 'm4' }),
    ).resolves.toBe(false)
    expect(show).not.toHaveBeenCalled()
  })

  it('幂等：同 dedupeKey 重复投递只决策一次（首次无标记不弹，重放带标记也不弹）', async () => {
    setDocumentState('visible', true)
    const show = vi.fn(async () => true)
    setBridge({ show })

    await expect(
      notifyPipelineMessage({ title: '灵汐', body: '正文', dedupeKey: 'm5' }),
    ).resolves.toBe(false)
    // 重放时窗口已切后台且补了标记：重放不是新活动，仍不弹
    setDocumentState('hidden', true)
    await expect(
      notifyPipelineMessage({ notifyWorthy: true, title: '灵汐', body: '正文', dedupeKey: 'm5' }),
    ).resolves.toBe(false)
    expect(show).not.toHaveBeenCalled()
  })

  it('空正文（纯工具调用轮）+ 标记：不弹且不消耗 dedupeKey', async () => {
    setDocumentState('hidden', true)
    const show = vi.fn(async () => true)
    setBridge({ show })

    await expect(
      notifyPipelineMessage({ notifyWorthy: true, title: '灵汐', body: '   ', dedupeKey: 'm6' }),
    ).resolves.toBe(false)
    expect(show).not.toHaveBeenCalled()
    // 同键补上正文：证明空正文首次未消耗幂等键
    await expect(
      notifyPipelineMessage({ notifyWorthy: true, title: '灵汐', body: '补上正文', dedupeKey: 'm6' }),
    ).resolves.toBe(true)
    expect(show).toHaveBeenCalledTimes(1)
  })

  it('桥接异常：折算 false 不上抛（消息流不被通知失败阻塞）', async () => {
    setDocumentState('hidden', true)
    setBridge({ show: vi.fn(async () => { throw new Error('ipc down') }) })

    await expect(
      notifyPipelineMessage({ notifyWorthy: true, title: '灵汐', body: '正文', dedupeKey: 'm7' }),
    ).resolves.toBe(false)
  })

  it('非 Electron（无桥接）：折算 false', async () => {
    setDocumentState('hidden', true)
    setBridge(undefined)

    await expect(
      notifyPipelineMessage({ notifyWorthy: true, title: '灵汐', body: '正文', dedupeKey: 'm8' }),
    ).resolves.toBe(false)
  })
})
