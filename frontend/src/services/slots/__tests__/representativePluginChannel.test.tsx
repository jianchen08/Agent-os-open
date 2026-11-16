/** @feature FP-T12 W 通用递送代表性测试通道 | @ci: frontend-test */
/**
 * 代表性前端测试通道（用户裁定：测完禁用；证据进台账
 * docs/working/DSH嫁接F域前端槽位_台账_20261104.md）
 *
 * 全链路：最小样例 widget 插件包（fixtures/sample_widget_plugin，真值源 =
 * 包内 plugin.json + frontend/main.js 源码）→ 按 SDK 约定构建产物
 * （esbuild，react external→宿主全局 shim，等价生产 rollup globals 映射）→
 * 经 W 通用加载器（loadPluginWidgets 真实链路，仅注入网络边界 importBundle）
 * 运行时注册 → contributes.slots 声明落 sidebar.block 与 shell.overlay →
 * 验证渲染 / fail-closed / ErrorBoundary 三行为 → 禁用（schema 重载摘除）。
 *
 * 不变式：SlotRegistry→loadPluginWidgets→createWidgetHostApi→widgetRegistry
 * 全链为生产代码；仅 dynamic import 的伺服 URL 以等价产物模块替身
 * （esbuild 产物经 data: URL 装载，非 mock 生产逻辑）。
 */

import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SlotHost } from '@/components/slots/SlotHost'
import { SlotOverlayHost } from '@/components/slots/SlotOverlayHost'
import { CommandDispatcher } from '@/services/schema/commandDispatcher'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { slotRegistry } from '@/services/slots/slotRegistry'
import { WIDGET_SDK_ESBUILD_CONFIG } from '@/services/slots/widgetSdk'

const fixtureRoot = join(dirname(fileURLToPath(import.meta.url)), 'fixtures', 'sample_widget_plugin')

/** 读取样例包真值源（plugin.json 为声明真值，main.js 为产物源码） */
function sampleManifest(): { id: string; contributes: Record<string, unknown[]> } {
  return JSON.parse(readFileSync(join(fixtureRoot, 'plugin.json'), 'utf-8'))
}

/**
 * 按 SDK 约定构建产物：react/antd external → 宿主全局 shim
 * （生产宿主把 React 挂 window；shim 即 external→globals 的构建期实现）。
 * 产物 = 自包含 ESM 文本（零 bare import），经 data: URL 动态装载。
 *
 * esbuild 是 node 侧库、测试进程是 jsdom realm（TextEncoder/Uint8Array 不变式
 * 冲突，esbuild 顶层 invariant 直接拒装）——构建在干净 node 子进程执行，
 * 产物文本经 stdout 回传。
 */
function buildSampleBundle(): string {
  const script = `
const esbuild = require('esbuild');
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const shim = {
  name: 'sdk-react-global-shim',
  setup(b) {
    b.onResolve({ filter: /^react$/ }, () => ({ path: 'react', namespace: 'sdk-global' }));
    b.onLoad({ filter: /.*/, namespace: 'sdk-global' }, () => ({
      contents: [
        'const R = window.React;',
        'const named = { Children: R.Children, Component: R.Component, createElement: R.createElement, Fragment: R.Fragment, StrictMode: R.StrictMode, useState: R.useState, useMemo: R.useMemo, useCallback: R.useCallback, useEffect: R.useEffect, useRef: R.useRef };',
        'export default R;',
        'export const { Children, Component, createElement, Fragment, StrictMode, useState, useMemo, useCallback, useEffect, useRef } = named;',
      ].join('\\n'),
      loader: 'js',
    }));
  },
};
esbuild
  .build({
    stdin: { contents: source, resolveDir: process.argv[2], sourcefile: 'main.js' },
    bundle: true,
    format: ${JSON.stringify(WIDGET_SDK_ESBUILD_CONFIG.format)},
    plugins: [shim],
    write: false,
    logLevel: 'silent',
  })
  .then((r) => process.stdout.write(r.outputFiles[0].text))
  .catch((e) => {
    console.error(String(e));
    process.exit(1);
  });
`
  const r = spawnSync(process.execPath, ['-e', script, join(fixtureRoot, 'frontend', 'main.js'), fixtureRoot], {
    cwd: join(fixtureRoot, '..', '..', '..', '..', '..'),
    encoding: 'utf8',
  })
  if (r.status !== 0 || !r.stdout) {
    throw new Error(`样例产物构建失败（SDK esbuild 链）：${r.stderr || r.stdout || 'no output'}`)
  }
  return r.stdout
}

/** plugin.json 内部形态（id/contributes）→ schema 装载形态（plugin_id/contributes） */
function sampleSchemaEntry(contributesOverride?: Record<string, unknown>): {
  plugin_id: string
  contributes: Record<string, unknown[]>
} {
  const m = sampleManifest()
  return {
    plugin_id: m.id,
    contributes: (contributesOverride ?? m.contributes) as Record<string, unknown[]>,
  }
}

async function loadSamplePlugin(bundle: string): Promise<void> {
  const dataUrl = `data:text/javascript;base64,${Buffer.from(bundle, 'utf-8').toString('base64')}`
  await slotRegistry.loadFromSchema(
    { plugin_contributes: [sampleSchemaEntry()] },
    // 仅网络边界替身：真实伺服 URL 换成等价产物模块的 data: URL 装载
    { importBundle: () => import(/* @vite-ignore */ dataUrl) },
  )
}

beforeEach(() => {
  widgetRegistry.clear()
  slotRegistry.clear()
  contributionRegistry.clear()
  delete (globalThis as Record<string, unknown>).__sampleWidgetApiShape
  delete (globalThis as Record<string, unknown>).__sampleWidgetTokenKeys
})

describe('代表性测试通道：样例 widget 插件包 → W 装入 → 槽位 → 禁用', () => {
  it('E2E：构建产物 → W 注册 → sidebar.block 渲染 → 声明 props 下发', async () => {
    const bundle = await buildSampleBundle()
    // 产物零 bare import（SDK 约定：宿主库 external）
    expect(bundle).not.toMatch(/from\s*["']react["']/)
    await loadSamplePlugin(bundle)

    // W 注册进动态命名空间
    expect(widgetRegistry.has('sample_widget/status_light')).toBe(true)
    expect(widgetRegistry.getEntry('sample_widget/status_light')?.metadata.source).toBe('plugin')
    // 注册 API 最小面：{registerWidget, tokens, bridge}，无全局对象/store/router
    expect((globalThis as Record<string, unknown>).__sampleWidgetApiShape).toEqual([
      'bridge',
      'registerWidget',
      'tokens',
    ])

    // sidebar.block 锚点渲染：声明 props 下发（组件只见 props）
    render(<SlotHost slotKey="sidebar.block" />)
    const light = screen.getByTestId('sample-status-light')
    expect(light.textContent).toContain('状态灯:on')
    expect(light.textContent).toContain('W 递送')
  })

  it('shell.overlay：命令触发 → overlay 条目模态呈现（modal 收敛同链）', async () => {
    contributionRegistry.loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'sample_widget',
          contributes: { commands: [{ id: 'sample_widget.announce', title: '公告' }] },
        },
      ],
    } as never)
    const dispatcher = new CommandDispatcher(contributionRegistry)
    dispatcher.setTransport(vi.fn().mockResolvedValue(undefined))
    const bundle = await buildSampleBundle()
    await loadSamplePlugin(bundle)

    render(<SlotOverlayHost dispatcher={dispatcher} />)
    expect(screen.queryByTestId('slot-overlay')).toBeNull()

    await dispatcher.executeCommand('sample_widget.announce')
    await waitFor(() => {
      expect(screen.getByTestId('slot-overlay')).toBeInTheDocument()
    })
    expect(screen.getByText('样例公告')).toBeInTheDocument()
    expect(screen.getByTestId('sample-announce').textContent).toContain('来自 W 递送')
  })

  it('fail-closed：表外槽 key / 引用不存在 widget = 装载报错跳过（条目粒度）', async () => {
    const bundle = await buildSampleBundle()
    const dataUrl = `data:text/javascript;base64,${Buffer.from(bundle, 'utf-8').toString('base64')}`
    const manifest = sampleManifest()
    // 坏声明混入：表外槽 key + 引用不存在的 widget（未随包注册）
    const badSlots = [
      ...((manifest.contributes.slots ?? []) as Array<Record<string, unknown>>),
      { id: 'bad_slot', slot: 'made.up.slot', channel: 'component', widget: 'sample_widget/status_light' },
      { id: 'ghost_widget', slot: 'sidebar.block', channel: 'component', widget: 'sample_widget/not_registered' },
    ]
    await slotRegistry.loadFromSchema(
      { plugin_contributes: [sampleSchemaEntry({ ...manifest.contributes, slots: badSlots })] },
      { importBundle: () => import(/* @vite-ignore */ dataUrl) },
    )

    // 合法条目存活；坏条目报错跳过（不静默空白、不渲染占位垃圾）
    const entries = slotRegistry.getSlotEntries('sidebar.block')
    expect(entries.map((e) => e.declId)).toEqual(['status_light_block', 'boom_block'])
    const errs = slotRegistry.getLoadErrors()
    expect(errs.some((e) => e.entryId?.includes('bad_slot') && e.reason.includes('表外'))).toBe(true)
    expect(errs.some((e) => e.entryId?.includes('ghost_widget') && e.reason.includes('not_registered'))).toBe(true)
    render(<SlotHost slotKey="sidebar.block" />)
    // 被跳过的坏声明无 DOM 痕迹（占位文本不含坏条目标识；boom_block 是合法
    // 声明的坏组件，其占位属 ErrorBoundary 行为通道，另测）
    expect(screen.queryByText(/not_registered/)).not.toBeInTheDocument()
    expect(screen.queryByText(/made\.up\.slot/)).not.toBeInTheDocument()
  })

  it('ErrorBoundary：坏 widget 只空一格，相邻条目存活（可见占位）', async () => {
    const bundle = await buildSampleBundle()
    await loadSamplePlugin(bundle)
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})

    render(<SlotHost slotKey="sidebar.block" />)
    // boom 条目（渲染即抛错）被边界隔离 → 可见占位
    expect(screen.getByTestId('slot-entry-error')).toBeInTheDocument()
    // 相邻 status_light 条目存活（单条目炸不拖塌布局）
    expect(screen.getByTestId('sample-status-light')).toBeInTheDocument()
    spy.mockRestore()
  })

  it('禁用：schema 重载不含该插件 → 槽条目与渲染零残留（测完禁用）', async () => {
    const bundle = await buildSampleBundle()
    await loadSamplePlugin(bundle)
    render(<SlotHost slotKey="sidebar.block" />)
    expect(screen.getByTestId('sample-status-light')).toBeInTheDocument()

    // 插件禁用语义：contributes 不再导出 → schema 重载槽条目摘除
    await slotRegistry.loadFromSchema(
      { plugin_contributes: [] },
      { importBundle: () => import(/* @vite-ignore */ 'data:text/javascript,') },
    )
    expect(slotRegistry.getSlotEntries('sidebar.block')).toHaveLength(0)
    expect(slotRegistry.getSlotEntries('shell.overlay')).toHaveLength(0)
    expect(screen.queryByTestId('sample-status-light')).not.toBeInTheDocument()
    expect(screen.queryByTestId('slot-host-sidebar.block')).toBeNull()
  })
})
