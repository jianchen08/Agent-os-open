/** @feature FP-T12 前端适配 | @ci: frontend-test */
/** @ci: frontend-test */
/**
 * AboutSettingsPage — 四项信息行（app 版本取壳注入 / 内核版本取 /health JSON /
 * 网关未透出降级 / 连接地址）。
 * API_BASE_URL 是模块加载期常量：须先布好 window 注入面再动态导入被测模块
 * （vi.resetModules 保证每次用例拿到按当轮环境求值的新常量）。
 */
import { screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'

const fetchMock = vi.hoisted(() => vi.fn())
vi.stubGlobal('fetch', fetchMock)
vi.stubGlobal('__LINGXI_BUILD__', { git: 'abc1234', time: '2026-10-13T00:00:00.000Z' })

beforeEach(() => {
  fetchMock.mockReset()
  vi.resetModules()
})

afterEach(() => {
  Object.defineProperty(window, 'Capacitor', { value: undefined, configurable: true })
  Object.defineProperty(window, 'LINGXI_SHELL_CONFIG', { value: undefined, configurable: true })
})

describe('AboutSettingsPage', () => {
  it('壳注入版本面 → 应用版本显示 versionName(versionCode)；四项各就位', async () => {
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      json: () => Promise.resolve({ status: 'ok', version: '0.2.10', timestamp: 't' }),
    })
    Object.defineProperty(window, 'LINGXI_SHELL_CONFIG', {
      value: { apiBase: 'http://cloud.example', versionName: '1.4', versionCode: 5 },
      configurable: true,
    })
    const { AboutSettingsPage } = await import('../AboutSettingsPage')
    renderWithProviders(<AboutSettingsPage embedded />)
    expect(screen.getByTestId('about-app-version')).toHaveTextContent('1.4 (5)')
    expect(screen.getByTestId('about-frontend-build')).toHaveTextContent('abc1234')
    await waitFor(() => {
      expect(screen.getByTestId('about-kernel-version')).toHaveTextContent('0.2.10')
    })
    expect(screen.getByTestId('about-server-base')).toHaveTextContent('http://cloud.example')
  })

  it('网关回落非 JSON（/health 未反代）→ 内核版本降级文案', async () => {
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      json: () => Promise.reject(new SyntaxError('not json')),
    })
    const { AboutSettingsPage } = await import('../AboutSettingsPage')
    renderWithProviders(<AboutSettingsPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('about-kernel-version')).toHaveTextContent('未透出')
    })
  })
})

describe('AboutSettingsPage — 诊断信息块（真机布局问题远程归因数据源）', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    vi.resetModules()
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      json: () => Promise.resolve({ status: 'ok', version: '0.2.10', timestamp: 't' }),
    })
    // 快照进页采集一次：每次用例前清掉上一轮可能写入的壳变量与主题 class
    document.documentElement.style.removeProperty('--safe-inset-top')
    document.documentElement.style.removeProperty('--safe-inset-bottom')
    document.documentElement.classList.remove('dark')
  })

  it('诊断块十二行各就位：安全区链路/视口/DPR/UA/主题', async () => {
    const { AboutSettingsPage } = await import('../AboutSettingsPage')
    renderWithProviders(<AboutSettingsPage embedded />)
    const testids = [
      'diag-shell-inset-top',
      'diag-shell-inset-bottom',
      'diag-safe-area-top',
      'diag-safe-area-bottom',
      'diag-env-inset-top',
      'diag-env-inset-bottom',
      'diag-env-inset-left',
      'diag-env-inset-right',
      'diag-viewport',
      'diag-dpr',
      'diag-ua',
      'diag-theme',
    ]
    for (const id of testids) {
      expect(screen.getByTestId(id), id).toBeInTheDocument()
    }
  })

  it('视口/DPR/UA 行透出真实运行值；壳变量未写入时显式标（壳未写入）', async () => {
    const { AboutSettingsPage } = await import('../AboutSettingsPage')
    renderWithProviders(<AboutSettingsPage embedded />)
    expect(screen.getByTestId('diag-viewport')).toHaveTextContent(
      `${window.innerWidth} × ${window.innerHeight}`,
    )
    expect(screen.getByTestId('diag-dpr')).toHaveTextContent(String(window.devicePixelRatio))
    expect(screen.getByTestId('diag-ua')).toHaveTextContent(navigator.userAgent)
    expect(screen.getByTestId('diag-shell-inset-top')).toHaveTextContent('壳未写入')
    expect(screen.getByTestId('diag-shell-inset-bottom')).toHaveTextContent('壳未写入')
  })

  it('壳已注入时透出实际值而非降级文案；主题行跟随 html.dark', async () => {
    document.documentElement.style.setProperty('--safe-inset-top', '37px')
    document.documentElement.style.setProperty('--safe-inset-bottom', '24px')
    document.documentElement.classList.add('dark')
    const { AboutSettingsPage } = await import('../AboutSettingsPage')
    renderWithProviders(<AboutSettingsPage embedded />)
    expect(screen.getByTestId('diag-shell-inset-top')).toHaveTextContent('37px')
    expect(screen.getByTestId('diag-shell-inset-bottom')).toHaveTextContent('24px')
    expect(screen.getByTestId('diag-theme')).toHaveTextContent('dark')
  })
})
