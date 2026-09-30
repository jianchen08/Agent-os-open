// @feature: FP-T12 前端适配(文件加载器注册表) | @ci: frontend-test
/**
 * 文件加载器路由测试（docs/working/文件加载器路由设计_20260928.md）。
 *
 * 行为契约：
 * - resolveLoader：扩展名主判（与页签渲染分派同源）、扩展名未知时 mime 兜底、
 *   插件声明覆盖内置、坏声明丢弃、全未命中 → null
 * - openFileWithLoader：全部 loader 统一走鉴权内容端点取流（text→文本 /
 *   image,pdf→blob），失败 → 报错 toast 且不开页签，未命中 → 「无对应的加载器」
 *   卡页签，已打开去重激活
 * - markdownLinkInterceptor：/uploads 链接拦截改走加载器，其余放行
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clearPluginFileLoaderBindings,
  markdownLinkInterceptor,
  openFileWithLoader,
  registerFileLoaderBindings,
  resolveLoader,
} from '@/services/fileLoaderRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { getFileEditorData } from '@/stores/fileEditorRegistry'

// mock sonner（外部 toast 库）：失败对用户可感知（错误 toast 弹出）
const toastErrorMock = vi.fn()
vi.mock('sonner', () => ({
  toast: { error: (...args: unknown[]) => toastErrorMock(...args) },
}))

// mock apiClient（外部 HTTP 依赖）：内容端点取流统一走适配层
const getMock = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => getMock(...args) },
}))

// blob URL（jsdom 无 createObjectURL，桩住断言传给页签数据的值）
const createObjectURLMock = vi.fn(() => 'blob:mock-1')
vi.stubGlobal('URL', {
  ...URL,
  createObjectURL: createObjectURLMock,
  revokeObjectURL: vi.fn(),
})

describe('resolveLoader', () => {
  afterEach(() => clearPluginFileLoaderBindings())

  it('扩展名主判：图片/pdf/文本各归其位', () => {
    expect(resolveLoader('pic.PNG')?.loader).toBe('image')
    expect(resolveLoader('doc.pdf')?.loader).toBe('pdf')
    expect(resolveLoader('main.py')?.loader).toBe('text')
    expect(resolveLoader('notes.md')?.loader).toBe('text')
  })

  it('扩展名未知时 mime 兜底', () => {
    expect(resolveLoader('README', 'text/plain')?.loader).toBe('text')
    expect(resolveLoader('blobbin', 'application/pdf')?.loader).toBe('pdf')
    expect(resolveLoader('blobbin', 'image/webp')?.loader).toBe('image')
  })

  it('全未命中 → null（.docx 波1 无内置加载器）', () => {
    expect(resolveLoader('报告.docx')).toBeNull()
    expect(resolveLoader('table.xlsx', 'application/vnd.ms-excel')).toBeNull()
  })

  it('插件声明覆盖内置（同扩展名）', () => {
    registerFileLoaderBindings([{ loader: 'text', extensions: ['.png'] }], 'my_plugin')
    const r = resolveLoader('a.png')
    expect(r?.loader).toBe('text')
    expect(r?.source).toBe('my_plugin')
  })

  it('插件可声明内置未覆盖的类型', () => {
    registerFileLoaderBindings([{ loader: 'text', extensions: ['.ndjson'] }], 'data_plugin')
    expect(resolveLoader('export.ndjson')?.loader).toBe('text')
  })

  it('坏声明丢弃：loader 不在词汇表内不注册、不崩', () => {
    registerFileLoaderBindings(
      [{ loader: 'not-a-loader' as 'text', extensions: ['.weird'] }],
      'bad_plugin',
    )
    expect(resolveLoader('f.weird')).toBeNull()
  })

  it('插件 mime 命中：扩展名未知时按声明 mime 归属', () => {
    registerFileLoaderBindings([{ loader: 'text', mimes: ['application/x-ndjson'] }], 'mime_plugin')
    expect(resolveLoader('export.data', 'application/x-ndjson')).toEqual({
      loader: 'text',
      source: 'mime_plugin',
    })
  })

  it('插件 mime 不命中：落回内置 mime 兜底（声明不吞内置面）', () => {
    registerFileLoaderBindings([{ loader: 'text', mimes: ['application/x-custom'] }], 'mime_plugin')
    expect(resolveLoader('blobbin', 'application/pdf')).toEqual({ loader: 'pdf', source: 'builtin' })
  })

  it('插件声明 extensions 不含该扩展名：该绑定不命中，落回内置', () => {
    registerFileLoaderBindings([{ loader: 'text', extensions: ['.ndjson'] }], 'ext_plugin')
    expect(resolveLoader('a.png')).toEqual({ loader: 'image', source: 'builtin' })
  })
})

describe('openFileWithLoader', () => {
  beforeEach(() => {
    getMock.mockReset()
    toastErrorMock.mockClear()
    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null, visitedTabIds: [] })
  })

  afterEach(() => {
    clearPluginFileLoaderBindings()
    vi.restoreAllMocks()
  })

  it('text：鉴权内容端点取文本（url basename 为键）并注册页签', async () => {
    getMock.mockResolvedValueOnce({ data: '文件内容' })

    await openFileWithLoader({ id: 'f1', name: 'notes.txt', url: '/uploads/abc123.txt' })

    expect(getMock).toHaveBeenCalledWith(
      '/ext/artifacts/files/abc123.txt',
      expect.objectContaining({ responseType: 'text', silent: true }),
    )
    const data = getFileEditorData('attach-f1')
    expect(data?.content).toBe('文件内容')
    const tab = useLayoutModeStore.getState().workspaceTabs[0]
    expect(tab.moduleId).toBe('__file_editor__')
    expect(tab.isActive).toBe(true)
  })

  it('image：取 blob 并把 objectURL 交给页签数据', async () => {
    getMock.mockResolvedValueOnce({ data: new Blob(['x']) })

    await openFileWithLoader({ name: 'pic.png', url: '/uploads/ab12.png' })

    expect(getMock).toHaveBeenCalledWith(
      '/ext/artifacts/files/ab12.png',
      expect.objectContaining({ responseType: 'blob', silent: true }),
    )
    expect(createObjectURLMock).toHaveBeenCalled()
    expect(getFileEditorData('attach-/uploads/ab12.png')?.url).toBe('blob:mock-1')
  })

  it('取流失败（404）：报错 toast 且不开页签', async () => {
    getMock.mockRejectedValueOnce(Object.assign(new Error('404'), { response: { status: 404 } }))

    await openFileWithLoader({ name: 'gone.txt', url: '/uploads/gone9.txt' })

    expect(toastErrorMock).toHaveBeenCalledTimes(1)
    expect(String(toastErrorMock.mock.calls[0][0])).toContain('gone.txt')
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(0)
  })

  it('网络失败：报错 toast（无 status 文案变体）且不开页签', async () => {
    getMock.mockRejectedValueOnce(new Error('network down'))

    await openFileWithLoader({ name: 'doc.pdf', url: '/uploads/nf77.pdf' })

    expect(toastErrorMock).toHaveBeenCalledTimes(1)
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(0)
  })

  it('未命中加载器（.docx）：开「无对应的加载器」卡页签，不取流', async () => {
    await openFileWithLoader({ name: '报告.docx', url: '/uploads/dd55.docx' })

    expect(getMock).not.toHaveBeenCalled()
    const data = getFileEditorData('attach-/uploads/dd55.docx')
    expect(data?.viewerOverride).toBe('none')
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)
  })

  it('已打开去重：第二次激活现有页签，不重复取流', async () => {
    getMock.mockResolvedValue({ data: '内容' })

    await openFileWithLoader({ id: 'dup', name: 'a.md', url: '/uploads/dup01.md' })
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)

    getMock.mockClear()
    await openFileWithLoader({ id: 'dup', name: 'a.md', url: '/uploads/dup01.md' })

    expect(getMock).not.toHaveBeenCalled()
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(1)
  })
})

describe('markdownLinkInterceptor', () => {
  beforeEach(() => {
    getMock.mockReset()
    toastErrorMock.mockClear()
    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null, visitedTabIds: [] })
  })

  afterEach(() => vi.restoreAllMocks())

  it('/uploads 链接：返回 true（阻止默认导航）并走加载器打开', async () => {
    getMock.mockResolvedValueOnce({ data: '内容' })
    const shouldHandle = markdownLinkInterceptor()

    expect(shouldHandle({ href: '/uploads/lnk01.txt', text: '说明.txt' })).toBe(true)

    await vi.waitFor(() => {
      expect(getMock).toHaveBeenCalledWith(
        '/ext/artifacts/files/lnk01.txt',
        expect.anything(),
      )
    })
    // 文件名取链接文本（原始名，而非 file_id 落盘名）
    expect(getFileEditorData('attach-/uploads/lnk01.txt')?.fileName).toBe('说明.txt')
  })

  it('非 /uploads 链接：返回 false（放行默认行为），不取流', () => {
    const shouldHandle = markdownLinkInterceptor()

    expect(shouldHandle({ href: 'https://example.com/a', text: '外链' })).toBe(false)
    expect(shouldHandle({ href: '/p/some-page', text: '站内' })).toBe(false)
    expect(getMock).not.toHaveBeenCalled()
  })
})
