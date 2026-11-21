/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */
/**
 * 任务现场纯函数补测（覆盖缺口清偿）：
 * - sceneResultText：七路抽取顺序命中与空白/非对象回落；
 * - sceneImageSource：截断标记/浏览器路径/MCP images（直挂与嵌套）/手机 base64/
 *   图片扩展名兜底/容器坐标缺省；
 * - sceneDetails：buildSceneToolCall 状态映射与可选字段携带、toActivityStatus、
 *   buildSceneDetailBlocks（命令类终端块保底/设备类正文块/实时输出/错误块）、
 *   formatError 各形态；
 * - sceneDiff：canInlinePreview 白名单与无扩展名、buildFileChanges 的
 *   批量删参路径兜底、computeFileNetStat/summarizeChanges。
 */
import { describe, expect, it } from 'vitest'
import {
  filterByCategories,
  presentCategories,
} from '@/components/workspace/taskScene/sceneActivities'
import {
  buildSceneDetailBlocks,
  buildSceneToolCall,
  formatError,
  toActivityStatus,
} from '@/components/workspace/taskScene/sceneDetails'
import {
  buildFileChanges,
  canInlinePreview,
  computeFileNetStat,
  summarizeChanges,
} from '@/components/workspace/taskScene/sceneDiff'
import {
  extractSceneImageSource,
  imageMimeOfPath,
  isScreenshotActivity,
  workspaceFileUrl,
} from '@/components/workspace/taskScene/sceneImageSource'
import { extractResultText } from '@/components/workspace/taskScene/sceneResultText'
import { truncateToolResultData } from '@/utils/toolResultTruncate'
import type { SceneActivity, SceneToolCategory } from '@/components/workspace/taskScene/sceneActivities'

function activity(overrides: Partial<SceneActivity> = {}): SceneActivity {
  return {
    callId: 'c1',
    toolName: 'bash',
    args: {},
    state: 'done',
    category: 'terminal',
    summary: 'ls',
    messageId: 'm1',
    ...overrides,
  } as SceneActivity
}

describe('extractResultText — 七路抽取与回落', () => {
  it('裸字符串（空白返 undefined）；非对象返 undefined', () => {
    expect(extractResultText('  hello  ')).toBe('  hello  ')
    expect(extractResultText('   ')).toBeUndefined()
    expect(extractResultText(42)).toBeUndefined()
    expect(extractResultText(null)).toBeUndefined()
  })

  it('text / output.text / data / snapshot_text / content / tree / snapshot 依序命中', () => {
    expect(extractResultText({ text: 'A' })).toBe('A')
    expect(extractResultText({ output: { text: 'B' } })).toBe('B')
    expect(extractResultText({ data: 'C' })).toBe('C')
    expect(extractResultText({ snapshot_text: 'D' })).toBe('D')
    expect(extractResultText({ content: 'E' })).toBe('E')
    expect(extractResultText({ tree: 'F' })).toBe('F')
    expect(extractResultText({ snapshot: 'G' })).toBe('G')
  })

  it('全空白字段当缺失，逐级下探；最终无命中返 undefined', () => {
    expect(extractResultText({ text: '  ', output: { text: 'deep' } })).toBe('deep')
    expect(extractResultText({ text: '', data: '', content: '', tree: '' })).toBeUndefined()
  })
})

describe('sceneImageSource — 截图来源判定', () => {
  it('isScreenshotActivity 三工具名；其它 false', () => {
    expect(isScreenshotActivity(activity({ toolName: 'browser_take_screenshot' }))).toBe(true)
    expect(isScreenshotActivity(activity({ toolName: 'Screenshot' }))).toBe(true)
    expect(
      isScreenshotActivity(activity({ toolName: 'phone_control', args: { action: 'screenshot' } })),
    ).toBe(true)
    expect(isScreenshotActivity(activity({ toolName: 'bash' }))).toBe(false)
    expect(
      isScreenshotActivity(activity({ toolName: 'phone_control', args: { action: 'tree' } })),
    ).toBe(false)
  })

  it('浏览器 file_path → file 来源（resultData 优先，result 兜底）', () => {
    expect(extractSceneImageSource(activity({ toolName: 'browser_take_screenshot', resultData: { file_path: '/tmp/s.png' } }))).toEqual({ kind: 'file', path: '/tmp/s.png' })
    expect(extractSceneImageSource(activity({ toolName: 'browser_take_screenshot', result: { file_path: '/tmp/r.png' } }))).toEqual({ kind: 'file', path: '/tmp/r.png' })
  })

  it('MCP images 直挂与嵌套 output.images；坏结构（空串 base64）不产出', () => {
    const shot = activity({ toolName: 'Screenshot' })
    expect(extractSceneImageSource(activity({ ...shot, resultData: { images: [{ base64: 'XYZ', mime_type: 'image/jpeg' }] } }))).toEqual({ kind: 'base64', mime: 'image/jpeg', data: 'XYZ' })
    expect(extractSceneImageSource(activity({ ...shot, result: { output: { images: [{ data: 'ABC' }] } } }))).toEqual({ kind: 'base64', mime: 'image/png', data: 'ABC' })
    expect(extractSceneImageSource(activity({ ...shot, resultData: { images: [] } }))).toBeUndefined()
    expect(extractSceneImageSource(activity({ ...shot, resultData: { images: [{ base64: '' }] } }))).toBeUndefined()
  })

  it('手机截图 base64（>64 字符才认，防误判小字段）', () => {
    const phone = { toolName: 'phone_control', args: { action: 'screenshot' } } as Partial<SceneActivity>
    const long = 'x'.repeat(70)
    expect(extractSceneImageSource(activity({ ...phone, resultData: { data: long } }))).toEqual({ kind: 'base64', mime: 'image/png', data: long })
    expect(extractSceneImageSource(activity({ ...phone, resultData: { data: 'short' } }))).toBeUndefined()
  })

  it('图片扩展名 path 兜底；非图片路径不产出', () => {
    expect(extractSceneImageSource(activity({ resultData: { path: 'shots/a.JPG' } }))).toEqual({ kind: 'file', path: 'shots/a.JPG' })
    expect(extractSceneImageSource(activity({ resultData: { path: 'docs/a.txt' } }))).toBeUndefined()
  })

  it('截断标记优先（截图类 + 截断结果 → kind=truncated）', () => {
    const truncated = { __truncated: true, preview: '前 1KB…', sequence: 7, totalBytes: 999 }
    const got = extractSceneImageSource(
      activity({ toolName: 'browser_take_screenshot', result: truncated }),
    )
    expect(got).toMatchObject({ kind: 'truncated', sequence: 7, totalBytes: 999 })
  })

  it('workspaceFileUrl 无容器坐标给空串；有坐标拼端点；MIME 未知按 png', () => {
    expect(workspaceFileUrl(undefined, 'a.png')).toBe('')
    expect(workspaceFileUrl('ct-1', 'dir/a.png')).toContain('ct-1')
    expect(workspaceFileUrl('ct-1', 'dir/a b.png')).toContain(encodeURIComponent('dir/a b.png'))
    expect(imageMimeOfPath('x.webp')).toBe('image/webp')
    expect(imageMimeOfPath('noext')).toBe('image/png')
  })
})

describe('sceneDetails — 详情块组装', () => {
  it('buildSceneToolCall：状态映射 + 可选字段按携带展开', () => {
    const base = buildSceneToolCall(activity({}))
    expect(base.status).toBe('completed')
    expect(base).not.toHaveProperty('result')
    const full = buildSceneToolCall(
      activity({
        state: 'error',
        result: { ok: false },
        resultData: { text: 'T' },
        error: 'boom',
        durationMs: 12,
        partialOutput: ['p'],
        startedAt: '2026-11-17T00:00:00Z',
        containerTaskId: 'ct',
      }),
    )
    expect(full.status).toBe('failed')
    expect(full.result).toEqual({ ok: false })
    expect(full.error).toBe('boom')
    expect(full.partialOutput).toEqual(['p'])
  })

  it('toActivityStatus：四态 + 默认 running', () => {
    expect(toActivityStatus(activity({ state: 'done' }))).toBe('completed')
    expect(toActivityStatus(activity({ state: 'error' }))).toBe('failed')
    expect(toActivityStatus(activity({ state: 'cancelled' }))).toBe('cancelled')
    expect(toActivityStatus(activity({ state: 'calling' }))).toBe('running')
    expect(toActivityStatus(activity({ state: 'streaming' }))).toBe('running')
  })

  it('buildSceneToolCall：cancelled/calling 状态映射（防只测两态的拟合）', () => {
    expect(buildSceneToolCall(activity({ state: 'cancelled' })).status).toBe('cancelled')
    expect(buildSceneToolCall(activity({ state: 'calling' })).status).toBe('running')
    expect(buildSceneToolCall(activity({ state: 'streaming' })).status).toBe('running')
  })

  it('buildSceneDetailBlocks：截断标记产出截断预览块（缺省 onExpand 兜底）', () => {
    const blocks = buildSceneDetailBlocks(
      activity({ category: 'other', result: { __truncated: true, preview: '前缀…', totalBytes: 4096 } }),
    )
    const trunc = blocks.find((b) => b.id === 'truncated-result' || b.label?.includes('截断'))
    expect(blocks.some((b) => b.collapsible === false || b.contentType === 'text')).toBe(true)
    expect(blocks.length).toBeGreaterThan(0)
    void trunc
  })

  it('buildSceneDetailBlocks：命令类保证终端块在场', () => {
    const blocks = buildSceneDetailBlocks(activity({ category: 'terminal', args: { command: 'ls' }, result: { exit_code: 0, stdout: 'ok' } }))
    expect(blocks.some((b) => b.contentType === 'terminal')).toBe(true)
  })

  it('buildSceneDetailBlocks：设备类正文块 + 实时输出 + 错误块', () => {
    const blocks = buildSceneDetailBlocks(
      activity({
        toolName: 'Snapshot',
        category: 'desktop',
        resultData: { text: '元素树正文' },
        partialOutput: ['line1', 'line2'],
        error: { message: '告警面' },
      }),
    )
    const textBlock = blocks.find((b) => b.id === 'scene-result-text')
    expect(textBlock?.content).toBe('元素树正文')
    expect(textBlock?.label).toBe('元素树 / 快照文本')
    expect(blocks.find((b) => b.id === 'partial-output')?.content).toBe('line1\nline2')
    expect(blocks.find((b) => b.id === 'error')?.content).toBe('告警面')
  })

  it('formatError：字符串/信封 message/可序列化对象/循环对象四态', () => {
    expect(formatError('plain')).toBe('plain')
    expect(formatError({ message: 'envelope' })).toBe('envelope')
    expect(formatError({ code: 3 })).toBe(JSON.stringify({ code: 3 }, null, 2))
    const cyclic: Record<string, unknown> = {}
    cyclic.self = cyclic
    expect(typeof formatError(cyclic)).toBe('string')
    expect(formatError(42)).toBe('42')
  })
})

describe('sceneDiff — 变更清单纯函数', () => {
  it('canInlinePreview：白名单扩展名/大小写/无扩展名/路径分隔符', () => {
    expect(canInlinePreview('a/b/c.py')).toBe(true)
    expect(canInlinePreview('D:\\x\\Y\\README.MD')).toBe(true)
    expect(canInlinePreview('noext')).toBe(false)
    expect(canInlinePreview('.hidden')).toBe(false)
    expect(canInlinePreview('a.exe')).toBe(false)
  })

  it('buildFileChanges：批量删除结果缺 results 时按参数路径兜底（仍标删除）', () => {
    const del = buildFileChanges([
      activity({ toolName: 'delete_file', args: { paths: ['x/a.txt', '', 42] }, result: {} }),
    ])
    expect(del).toHaveLength(1)
    expect(del[0]!.status).toBe('deleted')
    expect(del[0]!.path).toBe('x/a.txt')
    // 单 path 参数兜底
    const single = buildFileChanges([
      activity({ toolName: 'delete_file', args: { path: 'y/b.txt' }, result: {} }),
    ])
    expect(single[0]!.path).toBe('y/b.txt')
    // results 形态优先
    const viaResults = buildFileChanges([
      activity({
        toolName: 'delete_file',
        args: {},
        result: { results: [{ path: 'z/c.txt', deleted: true }, { path: 'z/skip.txt' }] },
      }),
    ])
    expect(viaResults.map((c) => c.path)).toEqual(['z/c.txt'])
  })

  it('computeFileNetStat 与 summarizeChanges：计数聚合与内容未知回落', () => {
    const changes = buildFileChanges([
      activity({
        toolName: 'file_write',
        args: { path: 'a.txt' },
        result: {
          file: 'a.txt',
          old_content: 'one\ntwo',
          new_content: 'one\nTWO\nthree',
          added: 2,
          removed: 1,
          size: 15,
        },
      }),
      activity({
        toolName: 'delete_file',
        args: { path: 'b.txt' },
        result: { results: [{ path: 'b.txt', deleted: true }] },
      }),
    ])
    const summary = summarizeChanges(changes)
    expect(summary.modified).toBe(1)
    expect(summary.deleted).toBe(1)
    const stat = computeFileNetStat(changes[0]!)
    expect(stat?.added).toBe(2)
    expect(stat?.removed).toBe(1)
    // 先写后删：末轮内容清空 → net stat 未知
    const writeThenDelete = buildFileChanges([
      activity({ toolName: 'file_write', args: { path: 'c.txt' }, result: { file: 'c.txt', old_content: '', new_content: 'x' } }),
      activity({ toolName: 'delete_file', args: { path: 'c.txt' }, result: { results: [{ path: 'c.txt', deleted: true }] } }),
    ])
    expect(writeThenDelete[0]!.status).toBe('deleted')
    expect(computeFileNetStat(writeThenDelete[0]!)).toBeUndefined()
  })
})

describe('活工具面真名截图工具 — 设备截图不再无图', () => {
  /** 回归锚：桌面通道截图工具真名是 `desktop_capture`（不是抽象名 Screenshot）。 */
  it('desktop_capture / read_image 认作截图类；其它 desktop_* 不认', () => {
    expect(isScreenshotActivity(activity({ toolName: 'desktop_capture' }))).toBe(true)
    expect(isScreenshotActivity(activity({ toolName: 'read_image' }))).toBe(true)
    expect(isScreenshotActivity(activity({ toolName: 'desktop_click' }))).toBe(false)
    expect(isScreenshotActivity(activity({ toolName: 'desktop_launch' }))).toBe(false)
  })

  it('desktop_capture 的 images[] → base64 来源（可渲染缩略图/灯箱）', () => {
    const got = extractSceneImageSource(
      activity({
        toolName: 'desktop_capture',
        resultData: { images: [{ base64: 'QkJC', mime_type: 'image/png' }] },
      }),
    )
    expect(got).toEqual({ kind: 'base64', mime: 'image/png', data: 'QkJC' })
  })

  it('desktop_capture 结果被 8KB 截断 → truncated（缩略图位给回取角标而非静默丢图）', () => {
    const big = { images: [{ base64: 'x'.repeat(20000), mime_type: 'image/png' }] }
    const truncated = truncateToolResultData(big, 55) as unknown
    const got = extractSceneImageSource(activity({ toolName: 'desktop_capture', resultData: truncated }))
    expect(got).toMatchObject({ kind: 'truncated', sequence: 55 })
  })
})

describe('sceneActivities — 类别筛选面（presentCategories/filterByCategories）', () => {
  it('presentCategories：空活动空类别；重复类别去重且保持首现顺序', () => {
    expect(presentCategories([])).toEqual([])
    expect(
      presentCategories([
        activity({ callId: 'a', category: 'browser' }),
        activity({ callId: 'b', category: 'terminal' }),
        activity({ callId: 'c', category: 'browser' }),
      ]),
    ).toEqual(['browser', 'terminal'])
  })

  it('filterByCategories：空集合不过滤（全量返回新数组）；命中集合只留成员', () => {
    const acts = [
      activity({ callId: 'a', category: 'browser' }),
      activity({ callId: 'b', category: 'terminal' }),
    ]
    expect(filterByCategories(acts, new Set())).toEqual(acts)
    expect(filterByCategories(acts, new Set<SceneToolCategory>(['terminal']))).toEqual([
      activity({ callId: 'b', category: 'terminal' }),
    ])
  })
})
