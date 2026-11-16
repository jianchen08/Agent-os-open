/** @feature FP-0.2.移动端适配 净装态 onboarding | @ci: frontend-test */
/**
 * onboarding 自动引导（services/onboarding/autoOpen.ts）行为契约：
 * - 触发驱动 = llm_setup 向导步骤（configure_llm）未完成：.env 无任何 *_API_KEY
 *   且持久化进度未 done 且 evaluator 求值未满足 → 打开 /p/get_started 并请求
 *   直达向导第 1 步（判定链不含任何路由前提，任意页面可弹）；
 * - 已存在 *_API_KEY = 已配置（现状口径，不引入 key 有效性校验）→ 不弹；
 * - 节流 = sessionStorage 会话旗标：每次应用启动至多自动弹一次（同次启动
 *   schema 事件重触发不重弹）；会话复位（下次启动）仍未完成 → 再弹；
 * - 插件未装载（页面未声明）/取数失败 → 静默降级：不弹、不抛、不落旗标
 *   （下次 schema 事件可重试）。
 *
 * 依赖注入全替身，网络与注册表零真实 IO。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { maybeAutoOpenOnboarding } from '../autoOpen'
import type { AutoOpenDeps } from '../autoOpen'
import type { Walkthrough } from '../types'

/** 与插件 content/walkthroughs/get_started.json 同构的最小夹具 */
function contentFixture(stepOverrides: Partial<Walkthrough['steps'][number]> = {}): Walkthrough[] {
  return [
    {
      id: 'get_started',
      title: '开始使用',
      description: '四步走通核心链',
      order: 1,
      default_open: true,
      steps: [
        { id: 'welcome', title: '欢迎', body: '正文', completion: { type: 'manual' } },
        {
          id: 'configure_llm',
          title: '配置模型与 API',
          body: '正文',
          wizard: 'llm_setup',
          completion: {
            type: 'all',
            conditions: [
              {
                type: 'api_check',
                endpoint: '/ext/llm_service/config/llm',
                json_path: 'providers',
                op: 'non_empty',
              },
              {
                type: 'api_check',
                endpoint: '/ext/llm_service/config/llm',
                json_path: 'defaults.chat',
                op: 'non_empty',
              },
            ],
          },
          ...stepOverrides,
        },
      ],
    },
  ]
}

function makeDeps(overrides: Partial<AutoOpenDeps> = {}): AutoOpenDeps {
  return {
    getPagePaths: vi.fn(() => ['/p/get_started', '/ext/other/ui']),
    getEnvVarSet: vi.fn(() => Promise.resolve(new Set<string>())),
    fetchContent: vi.fn(() => Promise.resolve({ walkthroughs: contentFixture() })),
    fetchProgress: vi.fn(() => Promise.resolve({ progress: {} })),
    fetchApiResult: vi.fn(() => Promise.resolve(undefined)),
    openByPath: vi.fn(() => true),
    requestFocus: vi.fn(),
    ...overrides,
  }
}

beforeEach(() => {
  sessionStorage.clear()
})

describe('触发判定（configure_llm 未完成驱动）', () => {
  it('(a) 净装无 key：打开引导页签并请求直达向导第 1 步', async () => {
    const deps = makeDeps()
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(true)
    expect(deps.openByPath).toHaveBeenCalledWith('/p/get_started')
    expect(deps.requestFocus).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem('lingxi.onboarding.autoopen.session.v2')).toBe('done')
  })

  it('非 *_API_KEY 后缀的变量不构成已配 key（性质边界：普通变量不算）', async () => {
    const deps = makeDeps({
      getEnvVarSet: vi.fn(() => Promise.resolve(new Set(['AGENTOS_BRIDGE_TOKEN', 'HOME']))),
    })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(true)
    expect(deps.openByPath).toHaveBeenCalledWith('/p/get_started')
  })

  it('sessionStorage 读取抛错（存储不可用）→ 视为无旗标，判定链照常走', async () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage blocked')
    })
    try {
      const deps = makeDeps()
      await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(true)
      expect(deps.openByPath).toHaveBeenCalledWith('/p/get_started')
    } finally {
      spy.mockRestore()
    }
  })

  it('内容无 llm_setup 向导步骤：无事可驱动，不开不聚焦', async () => {
    const deps = makeDeps({
      fetchContent: vi.fn(() =>
        Promise.resolve({ walkthroughs: contentFixture({ wizard: undefined }) }),
      ),
    })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(false)
    expect(deps.openByPath).not.toHaveBeenCalled()
    expect(deps.requestFocus).not.toHaveBeenCalled()
  })
})

describe('已配置/已完成不弹', () => {
  it('(c-1) .env 已含 *_API_KEY：不开不聚焦（存在即已配置，不取内容）', async () => {
    const deps = makeDeps({
      getEnvVarSet: vi.fn(() => Promise.resolve(new Set(['MINIMAX_API_KEY']))),
    })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(false)
    expect(deps.openByPath).not.toHaveBeenCalled()
    expect(deps.requestFocus).not.toHaveBeenCalled()
    expect(deps.fetchContent).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('lingxi.onboarding.autoopen.session.v2')).toBe('done')
  })

  it('(c-2) 持久化进度 configure_llm 已 done：不开', async () => {
    const deps = makeDeps({
      fetchProgress: vi.fn(() =>
        Promise.resolve({
          progress: { get_started: { configure_llm: { done: true, done_at: 1, how: 'all' } } },
        }),
      ),
    })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(false)
    expect(deps.openByPath).not.toHaveBeenCalled()
    expect(deps.requestFocus).not.toHaveBeenCalled()
  })

  it('(c-3) evaluator 双条件满足（providers + defaults.chat 非空）：不开', async () => {
    const deps = makeDeps({
      fetchApiResult: vi.fn(() =>
        Promise.resolve({ providers: { deepseek: {} }, defaults: { chat: 'deepseek-chat' } }),
      ),
    })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(false)
    expect(deps.openByPath).not.toHaveBeenCalled()
    expect(deps.requestFocus).not.toHaveBeenCalled()
  })

  it('evaluator 部分满足（providers 有值、defaults.chat 空）：仍未完成 → 弹', async () => {
    const deps = makeDeps({
      fetchApiResult: vi.fn(() =>
        Promise.resolve({ providers: { deepseek: {} }, defaults: { chat: '' } }),
      ),
    })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(true)
    expect(deps.openByPath).toHaveBeenCalledWith('/p/get_started')
  })
})

describe('会话节流（每启动至多一弹）', () => {
  it('(b-1) 同次启动重复判定（schema 事件重触发）不重弹、不再取数', async () => {
    const deps = makeDeps()
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(true)
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(false)
    expect(deps.openByPath).toHaveBeenCalledTimes(1)
    expect(deps.requestFocus).toHaveBeenCalledTimes(1)
    expect(deps.getEnvVarSet).toHaveBeenCalledTimes(1)
  })

  it('(b-2) 下次启动（会话复位）仍未完成 → 再弹', async () => {
    const deps = makeDeps()
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(true)
    sessionStorage.clear() // 模拟下次启动新会话
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(true)
    expect(deps.openByPath).toHaveBeenCalledTimes(2)
  })
})

describe('静默降级（不骚扰不崩）', () => {
  it('(d-1) 页面未声明（插件未装载）：不开不取数不落旗标；半途装载后可弹', async () => {
    const deps = makeDeps({ getPagePaths: vi.fn(() => ['/ext/other/ui']) })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(false)
    expect(deps.getEnvVarSet).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('lingxi.onboarding.autoopen.session.v2')).toBeNull()
    ;(deps.getPagePaths as ReturnType<typeof vi.fn>).mockReturnValue([
      '/p/get_started',
      '/ext/other/ui',
    ])
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(true)
    expect(deps.openByPath).toHaveBeenCalledWith('/p/get_started')
  })

  it('(d-2) 内容取数失败：不抛、不开、不落旗标；恢复后同会话可再判定', async () => {
    const deps = makeDeps({
      fetchContent: vi.fn(() => Promise.reject(new Error('plugin down'))),
    })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(false)
    expect(deps.openByPath).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('lingxi.onboarding.autoopen.session.v2')).toBeNull()
    ;(deps.fetchContent as ReturnType<typeof vi.fn>).mockResolvedValue({
      walkthroughs: contentFixture(),
    })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(true)
  })

  it('(d-3) env 掩码取数失败：不抛、不开、不落旗标（下次 schema 事件重试）', async () => {
    const deps = makeDeps({
      getEnvVarSet: vi.fn(() => Promise.reject(new Error('net down'))),
    })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(false)
    expect(deps.fetchContent).not.toHaveBeenCalled()
    expect(sessionStorage.getItem('lingxi.onboarding.autoopen.session.v2')).toBeNull()
  })

  it('api_check 端点失败 = 条件不满足（未取数端点语义）→ 仍弹', async () => {
    const deps = makeDeps({
      fetchApiResult: vi.fn(() => Promise.reject(new Error('llm config 500'))),
    })
    await expect(maybeAutoOpenOnboarding(deps)).resolves.toBe(true)
    expect(deps.openByPath).toHaveBeenCalledWith('/p/get_started')
  })
})
