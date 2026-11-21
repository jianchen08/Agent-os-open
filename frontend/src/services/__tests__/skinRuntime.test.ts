/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * skinRuntime 深分支测试（themeStore.skin.test.ts 覆盖 store 侧激活链的
 * 快乐路径，这里直测运行时服务本身的分支）：
 * - 危险 CSS 构造 fail-closed 拒绝注入（静态层零落地）
 * - CSS 拉取失败 → catch 日志，标签/信号已挂但不注入
 * - 消毒通过 → style 落地（命名空间属性 + plugin 归属 + nonce）
 * - 切换竞态代际守卫：迟到的旧响应不落地，不覆盖新皮肤
 * - 切换语义：先摘旧再装新（皮肤间直切不堆叠）
 * - clearPluginSkin 摘除面：样式/标签/暗色开关/背景信号/槽位变量全收
 * - 槽位预留：带文字 fixed 条带写入 --skin-chrome-*，无文字垂坠与窄条不预留
 * - hooks.mjs 空文本 → 跳过动态层，静态层不受影响
 *
 * 打桩边界：仅 apiClient（网络传输层）；sanitizeCss/getStyleNonce 真实现。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mockApiGet = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => ({
  apiClient: { get: (...args: unknown[]) => mockApiGet(...args) },
}))

import { applyPluginSkin, clearPluginSkin, isSkinTheme } from '../skinRuntime'

const OK_CSS = 'html[data-skin="p:ok"] .deco { position: absolute; top: 0; }'

/** 统计当前已注入的皮肤 style 元素 */
function skinStyles(): HTMLStyleElement[] {
  return Array.from(document.querySelectorAll('style[data-theme-style^="skin-"]'))
}

function themeOf(skin: string, base: 'light' | 'dark' = 'light') {
  return { pluginId: 'p', id: `t-${skin}`, name: skin, base, skin } as never
}

beforeEach(() => {
  mockApiGet.mockReset()
  mockApiGet.mockResolvedValue({ data: OK_CSS })
  clearPluginSkin()
  localStorage.clear()
})

afterEach(() => {
  clearPluginSkin()
  document.body.className = ''
  document.documentElement.removeAttribute('data-skin')
  document.documentElement.removeAttribute('style')
})

describe('isSkinTheme 声明判定', () => {
  it.each([
    { theme: null, expect: false },
    { theme: undefined, expect: false },
    { theme: {}, expect: false },
    { theme: { skin: '' }, expect: false },
    { theme: { skin: 'miku' }, expect: false }, // 缺 pluginId
    { theme: { skin: 'miku', pluginId: 'p' }, expect: true },
  ])('$# → $expect', ({ theme, expect: expected }) => {
    expect(isSkinTheme(theme as never)).toBe(expected)
  })
})

describe('applyPluginSkin 注入面', () => {
  it('消毒通过：style 落地，带命名空间属性/插件归属/nonce，暗色开关随底色', async () => {
    await applyPluginSkin(themeOf('ok', 'dark'))
    const styles = skinStyles()
    expect(styles).toHaveLength(1)
    expect(styles[0].getAttribute('data-theme-style')).toBe('skin-p:ok')
    expect(styles[0].getAttribute('data-plugin')).toBe('p')
    expect(styles[0].getAttribute('nonce')).toBeTruthy()
    expect(styles[0].textContent).toContain('.deco')
    expect(document.documentElement.getAttribute('data-skin')).toBe('p:ok')
    expect(document.body.hasAttribute('data-skin-dark')).toBe(true)
    expect(document.body.classList.contains('has-bg-image')).toBe(true)
  })

  it.each([
    { name: 'expression() 构造', css: 'button { width: expression(alert(1)); }' },
    { name: 'javascript: URL', css: 'button { background: url(javascript:alert(1)); }' },
    { name: '外部 @import', css: '@import url(https://evil.example/x.css);' },
    { name: 'behavior 构造', css: 'button { behavior: url(#default#userData); }' },
  ])('危险 CSS（$name）fail-closed：整段拒绝，style 不落地', async ({ css }) => {
    mockApiGet.mockResolvedValue({ data: css })
    await applyPluginSkin(themeOf('evil'))
    expect(skinStyles()).toHaveLength(0)
    // scope 打标与背景信号已发生（激活态），但样式零注入
    expect(document.documentElement.getAttribute('data-skin')).toBe('p:evil')
  })

  it('CSS 拉取失败：不抛出，style 不落地（静态层缺席不阻断页面）', async () => {
    mockApiGet.mockRejectedValue(new Error('网络断'))
    await expect(applyPluginSkin(themeOf('broken'))).resolves.toBeUndefined()
    expect(skinStyles()).toHaveLength(0)
  })

  it('hooks.mjs 空文本：跳过动态层，静态样式照常注入', async () => {
    mockApiGet.mockImplementation((url: string) => {
      if (String(url).endsWith('hooks.mjs')) return Promise.resolve({ data: '   ' })
      return Promise.resolve({ data: OK_CSS })
    })
    await applyPluginSkin(themeOf('ok'))
    expect(skinStyles()).toHaveLength(1)
  })
})

describe('切换竞态与摘除', () => {
  it('代际守卫：迟到的旧皮肤响应不落地，不覆盖新皮肤', async () => {
    let releaseOld!: (v: { data: string }) => void
    mockApiGet.mockImplementationOnce(
      () => new Promise<{ data: string }>((resolve) => { releaseOld = resolve }),
    )
    const oldApply = applyPluginSkin(themeOf('old'))
    await applyPluginSkin(themeOf('new'))
    releaseOld({ data: OK_CSS })
    await oldApply
    const styles = skinStyles()
    expect(styles).toHaveLength(1)
    expect(styles[0].getAttribute('data-theme-style')).toBe('skin-p:new')
    expect(document.documentElement.getAttribute('data-skin')).toBe('p:new')
  })

  it('皮肤间直切：先摘旧再装新，不堆叠', async () => {
    await applyPluginSkin(themeOf('first'))
    await applyPluginSkin(themeOf('second'))
    const styles = skinStyles()
    expect(styles).toHaveLength(1)
    expect(styles[0].getAttribute('data-theme-style')).toBe('skin-p:second')
  })

  it('clearPluginSkin 摘除面：样式/标签/暗色开关/背景信号/槽位变量全收', async () => {
    document.documentElement.style.setProperty('--skin-chrome-top', '40px')
    await applyPluginSkin(themeOf('ok', 'dark'))
    expect(skinStyles()).toHaveLength(1)
    clearPluginSkin()
    expect(skinStyles()).toHaveLength(0)
    expect(document.documentElement.getAttribute('data-skin')).toBeNull()
    expect(document.body.hasAttribute('data-skin-dark')).toBe(false)
    expect(document.body.classList.contains('has-bg-image')).toBe(false)
    expect(document.documentElement.style.getPropertyValue('--skin-chrome-top')).toBe('')
  })
})

describe('槽位预留（reserveChromeStrips）', () => {
  function mountStrip(opts: { edge: 'top' | 'bottom'; height: number; width: number; text: string }): HTMLElement {
    const el = document.createElement('div')
    el.setAttribute('data-test-strip', '')
    el.style.position = 'fixed'
    if (opts.edge === 'top') el.style.top = '0px'
    else el.style.bottom = '0px'
    el.innerText = opts.text
    el.getBoundingClientRect = () =>
      ({ height: opts.height, width: opts.width, top: 0, bottom: opts.height, left: 0, right: opts.width, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
    document.body.appendChild(el)
    return el
  }

  it('带文字的顶部 fixed 条带：量高写入 --skin-chrome-top', async () => {
    mountStrip({ edge: 'top', height: 40, width: 2000, text: '标题栏' })
    await applyPluginSkin(themeOf('ok'))
    await Promise.resolve()
    expect(document.documentElement.style.getPropertyValue('--skin-chrome-top')).toBe('40px')
  })

  it('无文字垂坠与半宽窄条不预留', async () => {
    mountStrip({ edge: 'top', height: 40, width: 2000, text: '   ' })
    mountStrip({ edge: 'bottom', height: 30, width: 100, text: '窄条' })
    await applyPluginSkin(themeOf('ok'))
    await Promise.resolve()
    expect(document.documentElement.style.getPropertyValue('--skin-chrome-top')).toBe('')
    expect(document.documentElement.style.getPropertyValue('--skin-chrome-bottom')).toBe('')
  })

  it('带文字的底部条带：量高写入 --skin-chrome-bottom', async () => {
    mountStrip({ edge: 'bottom', height: 28, width: 2000, text: '状态栏' })
    await applyPluginSkin(themeOf('ok'))
    await Promise.resolve()
    expect(document.documentElement.style.getPropertyValue('--skin-chrome-bottom')).toBe('28px')
  })

  afterEach(() => {
    document.querySelectorAll('[data-test-strip]').forEach((el) => el.remove())
  })
})

describe('图形垂坠让位（skin-chat-inset）', () => {
  /** 顶带：视口 0..height（几何基准，垂坠纠位钉到它的下沿） */
  function mountBand(height: number): HTMLElement {
    const el = document.createElement('div')
    el.setAttribute('data-testid', 'chat-top-band')
    el.setAttribute('data-test-band', '')
    el.getBoundingClientRect = () =>
      ({ height, width: 2000, top: 0, bottom: height, left: 0, right: 2000, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
    document.body.appendChild(el)
    return el
  }

  /** 列让位目标：列顶=顶带下沿、列底=视口底 */
  function mountTarget(top: number, height: number): HTMLElement {
    const el = document.createElement('div')
    el.setAttribute('data-skin-inset-target', 'chat')
    el.setAttribute('data-test-target', '')
    el.getBoundingClientRect = () =>
      ({ height, width: 900, top, bottom: top + height, left: 360, right: 1260, x: 360, y: top, toJSON: () => ({}) }) as DOMRect
    document.body.appendChild(el)
    return el
  }

  /** 图形垂坠条带（无文字）：rect 跟随 inline top（纠位后能量到新位置） */
  function mountGraphicStrip(opts: { edge: 'top' | 'bottom'; height: number; top: number }): HTMLElement {
    const el = document.createElement('div')
    el.setAttribute('data-test-strip', '')
    el.style.position = 'fixed'
    if (opts.edge === 'top') el.style.top = '0px'
    else el.style.bottom = '0px'
    el.innerText = '   '
    el.getBoundingClientRect = () => {
      const top = el.hasAttribute('data-skin-clamped-top') ? parseFloat(el.style.top) : opts.top
      return { height: opts.height, width: 2000, top, bottom: top + opts.height, left: 0, right: 2000, x: 0, y: top, toJSON: () => ({}) } as DOMRect
    }
    document.body.appendChild(el)
    return el
  }

  /** 带文字条栏（替代性标题栏/状态栏） */
  function mountTextStrip(height: number, text: string): HTMLElement {
    const el = document.createElement('div')
    el.setAttribute('data-test-strip', '')
    el.style.position = 'fixed'
    el.style.top = '0px'
    el.innerText = text
    el.getBoundingClientRect = () =>
      ({ height, width: 2000, top: 0, bottom: height, left: 0, right: 2000, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
    document.body.appendChild(el)
    return el
  }

  afterEach(() => {
    document.querySelectorAll('[data-test-strip], [data-test-band], [data-test-target]').forEach((el) => el.remove())
  })

  it('顶垂坠量测落 0：钉回顶带下沿 + 列写顶部让位', async () => {
    const band = mountBand(40)
    const target = mountTarget(40, 728)
    const strip = mountGraphicStrip({ edge: 'top', height: 76, top: 0 })
    await applyPluginSkin(themeOf('ok'))
    await Promise.resolve()
    expect(strip.style.top).toBe('40px')
    expect(strip.hasAttribute('data-skin-clamped-top')).toBe(true)
    expect(target.style.getPropertyValue('--skin-chat-inset-top')).toBe('76px')
    expect(document.documentElement.style.getPropertyValue('--skin-chrome-top')).toBe('')
    band.remove()
    target.remove()
  })

  it('顶垂坠已在顶带下沿：不纠位、让位高度=垂坠高', async () => {
    mountBand(40)
    const target = mountTarget(40, 728)
    const strip = mountGraphicStrip({ edge: 'top', height: 76, top: 40 })
    await applyPluginSkin(themeOf('ok'))
    await Promise.resolve()
    expect(strip.style.top).toBe('0px')
    expect(strip.hasAttribute('data-skin-clamped-top')).toBe(false)
    expect(target.style.getPropertyValue('--skin-chat-inset-top')).toBe('76px')
  })

  it('底垂坠可见：列写底部让位（只算与列相交段）', async () => {
    mountBand(40)
    const target = mountTarget(40, 728)
    mountGraphicStrip({ edge: 'bottom', height: 60, top: 708 })
    await applyPluginSkin(themeOf('ok'))
    await Promise.resolve()
    expect(target.style.getPropertyValue('--skin-chat-inset-bottom')).toBe('60px')
  })

  it('底垂坠退场（translateY 到视口外）：让位归零', async () => {
    mountBand(40)
    const target = mountTarget(40, 728)
    mountGraphicStrip({ edge: 'bottom', height: 60, top: 768 })
    await applyPluginSkin(themeOf('ok'))
    await Promise.resolve()
    expect(target.style.getPropertyValue('--skin-chat-inset-bottom')).toBe('')
  })

  it('带文字条栏走整根让位，不写列级让位', async () => {
    mountBand(40)
    const target = mountTarget(40, 728)
    mountTextStrip(40, '标题栏')
    await applyPluginSkin(themeOf('ok'))
    await Promise.resolve()
    expect(document.documentElement.style.getPropertyValue('--skin-chrome-top')).toBe('40px')
    expect(target.style.getPropertyValue('--skin-chat-inset-top')).toBe('')
  })

  it('摘除：列让位变量与纠位一起收回', async () => {
    mountBand(40)
    const target = mountTarget(40, 728)
    const strip = mountGraphicStrip({ edge: 'top', height: 76, top: 0 })
    await applyPluginSkin(themeOf('ok'))
    await Promise.resolve()
    expect(target.style.getPropertyValue('--skin-chat-inset-top')).toBe('76px')
    clearPluginSkin()
    expect(target.style.getPropertyValue('--skin-chat-inset-top')).toBe('')
    expect(strip.hasAttribute('data-skin-clamped-top')).toBe(false)
    expect(strip.style.top).toBe('')
  })

  it('状态变化重同步：body 属性变化 → 防抖后重量几何', async () => {
    mountBand(40)
    const target = mountTarget(40, 728)
    const strip = mountGraphicStrip({ edge: 'top', height: 76, top: 40 })
    await applyPluginSkin(themeOf('ok'))
    await Promise.resolve()
    expect(target.style.getPropertyValue('--skin-chat-inset-top')).toBe('76px')
    // 条带被皮肤挪出视口（退场态）→ 宿主状态属性变化触发重同步
    strip.getBoundingClientRect = () =>
      ({ height: 76, width: 2000, top: 768, bottom: 844, left: 0, right: 2000, x: 0, y: 768, toJSON: () => ({}) }) as DOMRect
    document.body.setAttribute('data-test-state', '1')
    await new Promise((resolve) => setTimeout(resolve, 800))
    expect(target.style.getPropertyValue('--skin-chat-inset-top')).toBe('')
    document.body.removeAttribute('data-test-state')
  })
})

describe('applyPluginSkin - 文本响应透传', () => {
  it('transformResponse 原样透传（axios 文本响应不被 JSON 化）', async () => {
    mockApiGet.mockImplementation(
      (_url: unknown, config?: { transformResponse?: Array<(d: string) => string> }) =>
        Promise.resolve({ data: config?.transformResponse?.[0]?.(OK_CSS) }),
    )
    await applyPluginSkin(themeOf('ok'))
    expect(skinStyles()).toHaveLength(1)
    expect(skinStyles()[0].textContent).toContain('.deco')
  })
})
