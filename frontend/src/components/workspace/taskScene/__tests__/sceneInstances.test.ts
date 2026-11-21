/** @feature FP-0.2.四 前端Schema（任务现场） @ci frontend-test */
/**
 * sceneInstances 派生纯函数补测：焦点取值边界（非法 URL/结果回显/软件多键
 * 回退）、在途判定、存活与计数派生、通道名映射单源（deriveSceneCategory）。
 * 断行为不断实现：两组有区分度输入 + 性质断言。
 */
import { describe, expect, it } from 'vitest'
import {
  buildSceneInstances,
  channelLabelOfTool,
  hostOf,
  instanceFocusOf,
  instanceStateOf,
  isInflight,
  liveInstances,
  totalActivities,
  unattachedCount,
} from '../sceneInstances'
import type { SceneActivity } from '../sceneActivities'

function activity(partial: Partial<SceneActivity> & Pick<SceneActivity, 'callId' | 'category'>): SceneActivity {
  return {
    toolName: partial.toolName ?? partial.category,
    args: {},
    state: 'done',
    summary: partial.summary ?? `${partial.category} 动作`,
    ...partial,
  } as SceneActivity
}

describe('hostOf · URL → host（非法输入不猜）', () => {
  it('合法 URL 取 host；空串/undefined/非法串 → undefined', () => {
    expect(hostOf('https://example.com/path')).toBe('example.com')
    expect(hostOf('')).toBeUndefined()
    expect(hostOf(undefined)).toBeUndefined()
    expect(hostOf('not a url')).toBeUndefined()
  })
})

describe('isInflight · 在途判定（calling/streaming 同义）', () => {
  it('calling 与 streaming 都在途；done/error/cancelled 不在途', () => {
    expect(isInflight(activity({ callId: 'a', category: 'terminal', state: 'calling' }))).toBe(true)
    expect(isInflight(activity({ callId: 'b', category: 'terminal', state: 'streaming' }))).toBe(true)
    expect(isInflight(activity({ callId: 'c', category: 'terminal', state: 'done' }))).toBe(false)
    expect(isInflight(activity({ callId: 'd', category: 'terminal', state: 'error' }))).toBe(false)
  })
})

describe('instanceFocusOf · 通道焦点取值', () => {
  it('浏览器：参数 url 优先，结果 resultData.url 兜底（结果回显场景）', () => {
    const fromArgs = activity({
      callId: 'b1',
      category: 'browser',
      toolName: 'browser_navigate',
      args: { url: 'https://args-first.com/x' },
    })
    expect(instanceFocusOf(fromArgs)).toBe('args-first.com')

    const fromResult = activity({
      callId: 'b2',
      category: 'browser',
      toolName: 'browser_take_screenshot',
      args: {},
      resultData: { url: 'https://from-result.com/y' },
    })
    expect(instanceFocusOf(fromResult)).toBe('from-result.com')
  })

  it('软件：app/file/url/name 逐键回退；手机与终端各取 action/command；其余空串', () => {
    expect(
      instanceFocusOf(activity({ callId: 's1', category: 'software', args: { name: 'WeChat' } })),
    ).toBe('WeChat')
    expect(
      instanceFocusOf(activity({ callId: 'p1', category: 'phone', args: { action: 'click' } })),
    ).toBe('click')
    expect(
      instanceFocusOf(
        activity({ callId: 't1', category: 'terminal', args: { command: 'npm test' } }),
      ),
    ).toBe('npm test')
    expect(instanceFocusOf(activity({ callId: 'o1', category: 'file' }))).toBe('')
  })
})

describe('buildSceneInstances · 实例派生与排序', () => {
  const calling = (id: string, category: SceneActivity['category'], startedAt?: string): SceneActivity =>
    activity({ callId: id, category, state: 'calling', ...(startedAt ? { startedAt } : {}) })

  it('管道运行中：在途实例存活且排在最前；同活按最早开始时间升序', () => {
    const instances = buildSceneInstances(
      [
        calling('t1', 'terminal', '2026-01-01T00:01:00Z'),
        calling('b1', 'browser', '2026-01-01T00:00:30Z'),
        activity({ callId: 'd1', category: 'desktop', state: 'done' }),
        activity({ callId: 'f1', category: 'file', state: 'done' }),
      ],
      { pipelineRunning: true },
    )
    // file 不成实例；活实例（browser 更早开跑）在前，死实例殿后
    expect(instances.map((i) => i.category)).toEqual(['browser', 'terminal', 'desktop'])
    expect(liveInstances(instances).map((i) => i.key)).toEqual(['browser', 'terminal'])
    expect(instanceStateOf(instances[0])).toBe('running')
    expect(unattachedCount([activity({ callId: 'x', category: 'other' })])).toBe(1)
    expect(totalActivities(instances)).toBe(3)
  })

  it('管道已结束：同一条在途调用不算运行中（遗留状态不冒充存活）；死实例按最近活动倒序', () => {
    const instances = buildSceneInstances(
      [
        calling('t1', 'terminal', '2026-01-01T00:01:00Z'),
        activity({
          callId: 'd1',
          category: 'desktop',
          state: 'done',
          startedAt: '2026-01-01T00:05:00Z',
        }),
      ],
      { pipelineRunning: false },
    )
    expect(liveInstances(instances)).toHaveLength(0)
    expect(instances.map((i) => i.category)).toEqual(['desktop', 'terminal'])
  })
})

describe('channelLabelOfTool · 类别映射单源', () => {
  it('工具名 → 通道文案走 utils/toolCategory 派生（bash→命令、file_write→文件、未知→其它）', () => {
    expect(channelLabelOfTool('bash')).toBe('命令')
    expect(channelLabelOfTool('file_write')).toBe('文件')
    expect(channelLabelOfTool('unknown_tool')).toBe('其它')
  })
})

