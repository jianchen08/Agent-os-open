/** @feature FP-0.2.四 声明驱动查看器路由 | @ci: frontend-test */
/**
 * fileViewerRegistry（声明驱动查看器路由，ADR 2026-10-24）单元测试
 *
 * - 自带内置表：图片/PDF/HTML 扩展名命中对应查看器，其余落默认文本
 * - 插件声明：extensions/mimes 绑定、优先于内置、schema 刷新全量重放
 * - 坏声明 fail-closed：结构违规整条丢弃（id/label/render.widget/extensions
 *   非法形态），不崩注册表
 * - file.open 下行事件：立即推 + 延迟重推
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  isPluginViewer,
  loadFileViewers,
  resolveFileViewer,
} from '@/services/fileViewerRegistry'

describe('fileViewerRegistry 路由', () => {
  beforeEach(() => {
    loadFileViewers([])
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('内置：图片扩展名 → image_preview，pdf → pdf_preview，html → html_preview', () => {
    expect(resolveFileViewer('a/photo.PNG').viewer.render.widget).toBe('image_preview')
    expect(resolveFileViewer('doc.pdf').viewer.render.widget).toBe('pdf_preview')
    expect(resolveFileViewer('page.htm').viewer.render.widget).toBe('html_preview')
    expect(isPluginViewer(resolveFileViewer('doc.pdf'))).toBe(false)
  })

  it('内置：未列出扩展名与无扩展名 → 默认文本编辑器兜底', () => {
    expect(resolveFileViewer('a/main.py').viewer.render.widget).toBe('code_editor')
    expect(resolveFileViewer('a/archive.xyz').viewer.render.widget).toBe('code_editor')
    expect(resolveFileViewer('Makefile').viewer.render.widget).toBe('code_editor')
  })

  it('插件声明：extensions 命中插件查看器（source=插件 id，优先于内置）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    loadFileViewers([
      {
        plugin_id: 'pptx_viewer',
        contributes: {
          file_viewers: [
            {
              id: 'pptx_viewer',
              label: 'PPT 预览',
              extensions: ['.pptx', '.ppt'],
              render: {
                widget: 'webview',
                props: { pluginId: 'pptx_viewer', htmlPath: '/page/pptx' },
              },
            },
          ],
        },
      },
    ])
    const resolved = resolveFileViewer('slides/演示.pptx')
    expect(resolved.viewer.id).toBe('pptx_viewer')
    expect(resolved.source).toBe('pptx_viewer')
    expect(resolved.viewer.render.widget).toBe('webview')
    expect(isPluginViewer(resolved)).toBe(true)
    warnSpy.mockRestore()
  })

  it('插件声明优先于内置：绑 .png 时 png 走插件（插件先于内置表路由）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    loadFileViewers([
      {
        plugin_id: 'fancy_img',
        contributes: {
          file_viewers: [
            { id: 'fancy_img', label: '增强图片', extensions: ['.png'], render: { widget: 'webview' } },
          ],
        },
      },
    ])
    expect(resolveFileViewer('a.png').source).toBe('fancy_img')
    warnSpy.mockRestore()
  })

  it('mime 绑定：扩展名未命中时 mime 兜底匹配（大小写不敏感）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    loadFileViewers([
      {
        plugin_id: 'dwg_viewer',
        contributes: {
          file_viewers: [
            {
              id: 'dwg_viewer',
              label: 'CAD 预览',
              extensions: ['.dwg'],
              mimes: ['application/acad'],
              render: { widget: 'webview' },
            },
          ],
        },
      },
    ])
    const resolved = resolveFileViewer('part.bin', 'Application/ACAD')
    expect(resolved.viewer.id).toBe('dwg_viewer')
    // 无 mime 时不参与匹配
    expect(resolveFileViewer('part.bin').viewer.render.widget).toBe('code_editor')
    warnSpy.mockRestore()
  })

  it('坏声明 fail-closed：缺 id/label/widget、extensions 非点前缀——整条丢弃', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    loadFileViewers([
      {
        plugin_id: 'bad_plugin',
        contributes: {
          file_viewers: [
            { id: '', label: 'x', extensions: ['.a'], render: { widget: 'webview' } },
            { id: 'v2', label: '', extensions: ['.b'], render: { widget: 'webview' } },
            { id: 'v3', label: 'x', extensions: ['.c'], render: { widget: '' } },
            { id: 'v4', label: 'x', extensions: ['c'], render: { widget: 'webview' } },
            { id: 'v5', label: 'x', extensions: '.c', render: { widget: 'webview' } },
            'garbage',
            // 合法条目不受坏邻居影响
            { id: 'good', label: 'ok', extensions: ['.ok'], render: { widget: 'webview' } },
          ],
        },
      },
    ])
    expect(warnSpy).toHaveBeenCalledTimes(6)
    expect(resolveFileViewer('f.c').viewer.render.widget).toBe('code_editor')
    expect(resolveFileViewer('f.ok').viewer.id).toBe('good')
    warnSpy.mockRestore()
  })

  it('schema 刷新全量重放：后一次装载替换前一次（禁用插件声明即失效）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    loadFileViewers([
      {
        plugin_id: 'p1',
        contributes: {
          file_viewers: [{ id: 'v1', label: 'x', extensions: ['.one'], render: { widget: 'webview' } }],
        },
      },
    ])
    expect(resolveFileViewer('f.one').viewer.id).toBe('v1')
    loadFileViewers([]) // p1 禁用 → 重放后声明消失
    expect(resolveFileViewer('f.one').viewer.render.widget).toBe('code_editor')
    warnSpy.mockRestore()
  })
})

describe('内置声明表行为等价（退役 fileEditors.ts 的删值实验锚）', () => {
  beforeEach(() => {
    loadFileViewers([])
  })

  it('旧 fileTypeMap 显式映射的代表扩展名 → 同一渲染组件', () => {
    const cases: Array<[string, string]> = [
      ['a.txt', 'code_editor'],
      ['a.md', 'code_editor'],
      ['a.json', 'code_editor'],
      ['a.yaml', 'code_editor'],
      ['a.svg', 'code_editor'],
      ['a.png', 'image_preview'],
      ['a.jpg', 'image_preview'],
      ['a.bmp', 'image_preview'],
      ['a.html', 'html_preview'],
    ]
    for (const [name, widget] of cases) {
      expect(resolveFileViewer(name).viewer.render.widget).toBe(widget)
    }
  })
})
