/** @feature FP-0.2.可观测性 dsh_adapter 插件（组件宿主） | @ci: frontend-test */
/**
 * DshSlotWidget 测试：三种如实状态（loading / error / ready）+ 空注册项态。
 *
 * applyClientBundle 经 vi.mock 桩接（类型导入不参与运行时，运行时只导出
 * applyClientBundle 一项）；锁组件渲染契约：缺参错误态、装载失败缺服务文案、
 * 槽位/contributionId 过滤、函数与元素两种注册项渲染、locale.t 命名空间绑定、
 * 卸载后 resolve 不回写状态（无 React 告警输出）。
 */
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/services/dshAdapter/clientHost', () => ({
  applyClientBundle: vi.fn(),
}))

import { applyClientBundle } from '@/services/dshAdapter/clientHost'
import { DshSlotWidget } from '../DshSlotWidget'
import type { DshBundleRuntime, DshSlotEntry } from '@/services/dshAdapter/clientHost'

const applyMock = vi.mocked(applyClientBundle)

/** 构造 DshBundleRuntime（缺省 applied=false 空面，按需覆盖）。 */
function runtimeOf(overrides: Partial<DshBundleRuntime> & { bundle: string }): DshBundleRuntime {
  return { applied: false, missingServices: [], entries: [], locale: null, ...overrides }
}

/** 构造 DshSlotEntry（id/order/localeNs 缺省空，按需覆盖）。 */
function entryOf(overrides: Partial<DshSlotEntry> & { name: string; component: unknown }): DshSlotEntry {
  return { id: null, order: null, localeNs: null, ...overrides }
}

beforeEach(() => {
  applyMock.mockReset()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('DshSlotWidget', () => {
  it('缺 bundle / slot → 错误态「页面声明缺 bundle / slot」', async () => {
    const missingSlot = render(<DshSlotWidget slot="sidebar.right.pane.tab" />)
    expect(await missingSlot.findByText('页面声明缺 bundle / slot')).toBeInTheDocument()
    missingSlot.unmount()

    const missingBundle = render(<DshSlotWidget bundle="dsh-desktop-sidecar" />)
    expect(await missingBundle.findByText('页面声明缺 bundle / slot')).toBeInTheDocument()
    missingBundle.unmount()

    render(<DshSlotWidget />)
    expect(await screen.findByText('页面声明缺 bundle / slot')).toBeInTheDocument()
    // 缺参不走 bundle 装载
    expect(applyMock).not.toHaveBeenCalled()
  })

  it('装载中先渲染 loading 文案，applied=false 显示 missingServices join("；")', async () => {
    applyMock.mockResolvedValue(
      runtimeOf({ bundle: 'pkg-a', applied: false, missingServices: ['dep:x', '', 'dep:y'] }),
    )
    const { unmount } = render(<DshSlotWidget bundle="pkg-a" slot="s" />)
    // 首帧 loading（resolve 前的同步渲染）
    expect(screen.getByText(/正在装载插件组件（pkg-a）/)).toBeInTheDocument()
    // 错误态：缺失服务按「；」join（filter(Boolean) 剔除空串）
    expect(await screen.findByText('dep:x；dep:y')).toBeInTheDocument()
    expect(screen.getByText('插件组件未能装载：pkg-a')).toBeInTheDocument()
    unmount()
  })

  it('applied=false 且 missingServices 为空 → 回退「client bundle 装载失败」', async () => {
    applyMock.mockResolvedValue(runtimeOf({ bundle: 'pkg-b', applied: false, missingServices: [] }))
    render(<DshSlotWidget bundle="pkg-b" slot="s" />)
    expect(await screen.findByText('client bundle 装载失败')).toBeInTheDocument()
  })

  it('applied=true 且过滤后无注册项 → 空态「未在槽位注册组件」', async () => {
    applyMock.mockResolvedValue(
      runtimeOf({
        bundle: 'pkg-a',
        applied: true,
        entries: [entryOf({ name: 'other.slot', component: () => null })],
      }),
    )
    render(<DshSlotWidget bundle="pkg-a" slot="s" />)
    expect(await screen.findByText(/未在槽位 s 注册组件/)).toBeInTheDocument()
    expect(screen.queryByText(/正在装载插件组件/)).not.toBeInTheDocument()
  })

  it('函数注册项 → 渲染组件，locale.t 按 entry.localeNs 命名空间绑定', async () => {
    const tMock = vi.fn(() => '桌宠面板')
    function Greet({ t }: { t?: (key: string, params?: Record<string, unknown>) => string }) {
      return <div data-testid="fn-entry">{t ? t('tab.title', { who: '灵' }) : 'no-t'}</div>
    }
    applyMock.mockResolvedValue(
      runtimeOf({
        bundle: 'pkg-a',
        applied: true,
        entries: [entryOf({ name: 's', id: 'greet', component: Greet, localeNs: 'petcare' })],
        locale: { t: tMock } as unknown as DshBundleRuntime['locale'],
      }),
    )
    render(<DshSlotWidget bundle="pkg-a" slot="s" />)
    expect(await screen.findByTestId('fn-entry')).toHaveTextContent('桌宠面板')
    // props.t 转发为 locale.t(ns, key, params)
    expect(tMock).toHaveBeenCalledTimes(1)
    expect(tMock).toHaveBeenCalledWith('petcare', 'tab.title', { who: '灵' })
  })

  it('React 元素注册项（$$typeof）→ 原样渲染', async () => {
    applyMock.mockResolvedValue(
      runtimeOf({
        bundle: 'pkg-a',
        applied: true,
        entries: [entryOf({ name: 's', id: 'elem', component: <div key="elem-entry" data-testid="elem-entry">原样元素</div> })],
      }),
    )
    render(<DshSlotWidget bundle="pkg-a" slot="s" />)
    expect(await screen.findByTestId('elem-entry')).toHaveTextContent('原样元素')
  })

  it('component 既非函数非元素 → 容器无子节点（渲染 null）', async () => {
    applyMock.mockResolvedValue(
      runtimeOf({
        bundle: 'pkg-a',
        applied: true,
        entries: [entryOf({ name: 's', id: 'junk', component: 42 })],
      }),
    )
    const { container } = render(<DshSlotWidget bundle="pkg-a" slot="s" />)
    await waitFor(() => expect(container.querySelector('[data-dsh-slot="s"]')).toBeInTheDocument())
    const slotBox = container.querySelector('[data-dsh-slot="s"]')!
    expect(slotBox.dataset.dshBundle).toBe('pkg-a')
    expect(slotBox.children).toHaveLength(0)
    expect(slotBox.textContent).toBe('')
  })

  it('contributionId 过滤：只渲染匹配 id 且槽位相同的注册项', async () => {
    const CompA = () => <div data-testid="entry-a" />
    const CompB = () => <div data-testid="entry-b" />
    applyMock.mockResolvedValue(
      runtimeOf({
        bundle: 'pkg-a',
        applied: true,
        entries: [
          entryOf({ name: 's', id: 'first', component: CompA }),
          entryOf({ name: 's', id: 'steer', component: CompB }),
          entryOf({ name: 'other.slot', id: 'steer', component: CompA }),
        ],
      }),
    )
    render(<DshSlotWidget bundle="pkg-a" slot="s" contributionId="steer" />)
    await screen.findByTestId('entry-b')
    expect(screen.queryByTestId('entry-a')).not.toBeInTheDocument()
    expect(screen.queryByText(/未在槽位/)).not.toBeInTheDocument()
  })

  it('卸载后 resolve → 不回写状态、无 React 告警输出', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    let resolveA!: (runtime: DshBundleRuntime) => void
    applyMock.mockImplementation((bundle: string) => {
      if (bundle === 'pkg-a') {
        return new Promise<DshBundleRuntime>((resolve) => {
          resolveA = resolve
        })
      }
      return Promise.resolve(
        runtimeOf({
          bundle: 'pkg-b',
          applied: true,
          entries: [entryOf({ name: 's', id: 'b', component: () => <div data-testid="entry-b" /> })],
        }),
      )
    })
    const { unmount } = render(<DshSlotWidget bundle="pkg-a" slot="s" />)
    unmount()
    // 卸载后旧 promise 才 resolve：alive=false → 状态不更新
    resolveA(
      runtimeOf({
        bundle: 'pkg-a',
        applied: true,
        entries: [entryOf({ name: 's', id: 'a', component: () => <div data-testid="entry-a" /> })],
      }),
    )
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.queryByTestId('entry-a')).not.toBeInTheDocument()
    expect(consoleError).not.toHaveBeenCalled()
    consoleError.mockRestore()
  })

  it('rerender 换 bundle 后旧 promise resolve → 不覆盖新 bundle 的状态', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    let resolveA!: (runtime: DshBundleRuntime) => void
    applyMock.mockImplementation((bundle: string) => {
      if (bundle === 'pkg-a') {
        return new Promise<DshBundleRuntime>((resolve) => {
          resolveA = resolve
        })
      }
      return Promise.resolve(
        runtimeOf({
          bundle: 'pkg-b',
          applied: true,
          entries: [entryOf({ name: 's', id: 'b', component: () => <div data-testid="entry-b" /> })],
        }),
      )
    })
    const { rerender } = render(<DshSlotWidget bundle="pkg-a" slot="s" />)
    // 切到 pkg-b：旧 effect 清理（alive=false），新 bundle ready
    rerender(<DshSlotWidget bundle="pkg-b" slot="s" />)
    expect(await screen.findByTestId('entry-b')).toBeInTheDocument()
    // 旧 promise 迟到 resolve：不回写状态
    resolveA(
      runtimeOf({
        bundle: 'pkg-a',
        applied: true,
        entries: [entryOf({ name: 's', id: 'a', component: () => <div data-testid="entry-a" /> })],
      }),
    )
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.queryByTestId('entry-a')).not.toBeInTheDocument()
    expect(screen.getByTestId('entry-b')).toBeInTheDocument()
    expect(consoleError).not.toHaveBeenCalled()
    consoleError.mockRestore()
  })
})
