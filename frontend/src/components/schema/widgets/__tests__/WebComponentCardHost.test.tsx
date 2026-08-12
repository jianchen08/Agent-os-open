/**
 * WebComponentCardHost 测试 — 阶段 4 第二条组件注入路径
 *
 * 背景：
 * - 插件提供一个 JS 文件（注册成 Custom Element），前端动态加载 + 执行 +
 *   把 props 传给元素。比 iframe/webview 轻（同进程、直接传 props、不走 postMessage）。
 * - JS 来源：`/ext/${pluginId}${scriptPath}`（插件 http.handle 返回 JS，后端 dispatcher 透传）。
 * - 鉴权：apiClient 自带 Bearer；插件代码经准入白名单（第一阶段安全假设，见组件注释）。
 *
 * jsdom 兼容：实测 jsdom 原生支持 customElements（define/get/createElement/connectedCallback），
 * 故此处直接用真实 registry，仅用唯一 tagName 隔离各用例（customElements 无法 undefine）。
 *
 * 可观察行为（AC）：
 * - AC-1: 未注册时 → apiClient.get(`/ext/${pluginId}${scriptPath}`) 被调用（拼接正确）
 * - AC-2: 脚本被执行 → customElements.get(tagName) 返回已定义类
 * - AC-3: tagName 元素被 append 到容器（connectedCallback 触发）
 * - AC-4: props 被设到元素上（作为 DOM 属性）
 * - AC-5: 已注册时 → 跳过 fetch，仍渲染元素
 * - AC-6: fetch 失败 → 显示错误占位（不崩溃）
 * - AC-7: 缺 pluginId 或 tagName → 错误占位（不调 fetch）
 * - AC-8: 脚本执行抛错 → 错误占位
 */
import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

import { apiClient } from '@/services/api/client'

// ── Mock 外部依赖 ──
vi.mock('@/services/api/client', () => ({
  apiClient: {
    get: vi.fn(),
  },
}))

import { WebComponentCardHost } from '../WebComponentCardHost'

const apiGet = apiClient.get as unknown as Mock

/** 全局自增 seq，给每个用例生成唯一 tagName（customElements 无法 undefine）。 */
let seq = 0
function uniqueTag(prefix = 't-wc'): string {
  seq += 1
  return `${prefix}-${seq}`
}

/** 生成一段「定义指定 tagName 的 Custom Element」的 JS 脚本。 */
function defineScript(tag: string): string {
  return `
    customElements.define(${JSON.stringify(tag)}, class extends HTMLElement {
      connectedCallback() { this.dataset.connected = 'true'; }
      disconnectedCallback() { this.dataset.disconnected = 'true'; }
    });
    window.__lastDefinedTag = ${JSON.stringify(tag)};
  `
}

describe('WebComponentCardHost', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('AC-1: 未注册时 → 用拼接的 /ext/{pluginId}{scriptPath} fetch', async () => {
    const tag = uniqueTag()
    apiGet.mockResolvedValue({ data: defineScript(tag) })

    const { unmount } = render(
      <WebComponentCardHost pluginId="demo" scriptPath="/component.js" tagName={tag} />,
    )

    await waitFor(() => {
      expect(apiGet).toHaveBeenCalledWith('/ext/demo/component.js', expect.anything())
    })
    unmount()
  })

  it('AC-1b: scriptPath 拼接：无前导 / 时自动补 /', async () => {
    const tag = uniqueTag()
    apiGet.mockResolvedValue({ data: defineScript(tag) })

    const { unmount } = render(
      <WebComponentCardHost pluginId="demo" scriptPath="component.js" tagName={tag} />,
    )

    await waitFor(() => {
      expect(apiGet).toHaveBeenCalledWith('/ext/demo/component.js', expect.anything())
    })
    unmount()
  })

  it('AC-2: 脚本被执行 → customElements.get(tagName) 返回已定义类', async () => {
    const tag = uniqueTag()
    apiGet.mockResolvedValue({ data: defineScript(tag) })

    const { unmount } = render(
      <WebComponentCardHost pluginId="demo" scriptPath="/c.js" tagName={tag} />,
    )

    await waitFor(() => {
      // globalThis.__lastDefinedTag is asserted via customElements.get for the contract.
      expect(customElements.get(tag)).toBeTypeOf('function')
    })
    unmount()
  })

  it('AC-3: tagName 元素被 append 到容器（connectedCallback 触发）', async () => {
    const tag = uniqueTag()
    apiGet.mockResolvedValue({ data: defineScript(tag) })

    const { container, unmount } = render(
      <WebComponentCardHost pluginId="demo" scriptPath="/c.js" tagName={tag} />,
    )

    await waitFor(() => {
      const el = container.querySelector(tag) as HTMLElement | null
      expect(el).not.toBeNull()
      expect(el?.dataset.connected).toBe('true')
    })
    unmount()
  })

  it('AC-4: props 被设到元素上（作为 DOM 属性）', async () => {
    const tag = uniqueTag()
    apiGet.mockResolvedValue({ data: defineScript(tag) })

    const props = { greeting: 'hi', count: 7, nested: { a: 1 } }
    const { container, unmount } = render(
      <WebComponentCardHost
        pluginId="demo"
        scriptPath="/c.js"
        tagName={tag}
        props={props}
      />,
    )

    await waitFor(() => {
      const el = container.querySelector(tag) as HTMLElement | null
      expect(el).not.toBeNull()
      // 复杂对象作为 DOM 属性（property）直接赋值；标量也走属性
      expect((el as unknown as Record<string, unknown>).greeting).toBe('hi')
      expect((el as unknown as Record<string, unknown>).count).toBe(7)
      expect((el as unknown as Record<string, unknown>).nested).toEqual({ a: 1 })
    })
    unmount()
  })

  it('AC-5: 已注册时 → 跳过 fetch，仍渲染元素', async () => {
    const tag = uniqueTag()
    // 预先注册（模拟另一实例已加载脚本）
    customElements.define(tag, class extends HTMLElement {})

    apiGet.mockResolvedValue({ data: defineScript(tag) })

    const { container, unmount } = render(
      <WebComponentCardHost pluginId="demo" scriptPath="/c.js" tagName={tag} />,
    )

    // 元素应被渲染（不等 fetch）
    await waitFor(() => {
      expect(container.querySelector(tag)).not.toBeNull()
    })
    // 给微任务一个窗口，确认不会触发 fetch
    await new Promise((r) => setTimeout(r, 30))
    expect(apiGet).not.toHaveBeenCalled()
    unmount()
  })

  it('AC-6: fetch 失败 → 显示错误占位（不崩溃）', async () => {
    const tag = uniqueTag()
    apiGet.mockRejectedValue(new Error('network down'))

    const { unmount } = render(
      <WebComponentCardHost pluginId="demo" scriptPath="/c.js" tagName={tag} />,
    )

    await waitFor(() => {
      expect(screen.getByText(/加载失败|network down/i)).toBeInTheDocument()
    })
    unmount()
  })

  it('AC-7: 缺 pluginId → 错误占位，不调 fetch', async () => {
    const tag = uniqueTag()
    const { unmount } = render(
      // @ts-expect-error 故意缺 pluginId
      <WebComponentCardHost scriptPath="/c.js" tagName={tag} />,
    )

    await waitFor(() => {
      expect(screen.getByText(/pluginId|scriptPath|tagName/i)).toBeInTheDocument()
    })
    expect(apiGet).not.toHaveBeenCalled()
    unmount()
  })

  it('AC-7b: 缺 tagName → 错误占位，不调 fetch', async () => {
    const { unmount } = render(
      // @ts-expect-error 故意缺 tagName
      <WebComponentCardHost pluginId="demo" scriptPath="/c.js" />,
    )

    await waitFor(() => {
      expect(screen.getByText(/pluginId|scriptPath|tagName/i)).toBeInTheDocument()
    })
    expect(apiGet).not.toHaveBeenCalled()
    unmount()
  })

  it('AC-8: 脚本执行抛错 → 错误占位', async () => {
    const tag = uniqueTag()
    apiGet.mockResolvedValue({ data: 'throw new Error("bad script")' })

    const { unmount } = render(
      <WebComponentCardHost pluginId="demo" scriptPath="/c.js" tagName={tag} />,
    )

    await waitFor(() => {
      expect(screen.getByText(/加载失败|bad script/i)).toBeInTheDocument()
    })
    unmount()
  })
})
