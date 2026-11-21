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
    // .bin 本轮声明为 binary_viewer 兜底（不再落文本编辑器吐乱码）；未声明扩展名仍走默认文本编辑器
    expect(resolveFileViewer('part.bin').viewer.render.widget).toBe('binary_notice')
    expect(resolveFileViewer('part.unknown').viewer.render.widget).toBe('code_editor')
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
      // 本轮有意变更：.md → markdown 预览（渲染态，源码可在查看器内切）
      ['a.md', 'markdown_preview'],
      ['a.json', 'code_editor'],
      ['a.yaml', 'code_editor'],
      // 本轮有意变更：.svg → 图片查看器（矢量浏览器原生可渲染）
      ['a.svg', 'image_preview'],
      ['a.png', 'image_preview'],
      ['a.jpg', 'image_preview'],
      ['a.bmp', 'image_preview'],
      ['a.html', 'html_preview'],
      ['a.mp4', 'media_preview'],
      ['a.mp3', 'media_preview'],
      ['a.zip', 'binary_notice'],
      ['a.exe', 'binary_notice'],
    ]
    for (const [name, widget] of cases) {
      expect(resolveFileViewer(name).viewer.render.widget).toBe(widget)
    }
  })
})

describe('内置声明表：Excel（excel_viewer，选型接入新增）', () => {
  beforeEach(() => {
    loadFileViewers([])
  })

  it('xlsx/xlsm/xls 命中 excel_preview（builtin 来源，大小写不敏感）', () => {
    for (const name of ['a.xlsx', 'b.XLSX', 'c.xlsm', 'd.xls']) {
      const resolved = resolveFileViewer(name)
      expect(resolved.viewer.render.widget).toBe('excel_preview')
      expect(resolved.source).toBe('builtin')
    }
  })

  it('近似扩展名不误伤：.xlsb 未声明走默认文本编辑器，复合扩展名按尾段判定', () => {
    expect(resolveFileViewer('a.xlsb').viewer.render.widget).toBe('code_editor')
    expect(resolveFileViewer('a.xlsx.txt').viewer.render.widget).toBe('code_editor')
  })
})

describe('内置声明表：既有类型路由命中补证（接入类型 × 路由全覆盖）', () => {
  beforeEach(() => {
    loadFileViewers([])
  })

  it('docx/pptx → document_viewer（可批注文档查看器）；pdf 仍优先命中 pdf_viewer', () => {
    expect(resolveFileViewer('报告.docx').viewer.render.widget).toBe('document_viewer')
    expect(resolveFileViewer('slides/演示.PPTX').viewer.render.widget).toBe('document_viewer')
    // .pdf 在声明表中先被 pdf_viewer 命中（声明序语义，不得改动）
    expect(resolveFileViewer('doc.pdf').viewer.render.widget).toBe('pdf_preview')
  })

  it('markdown 双扩展名与媒体全集合命中既有查看器', () => {
    expect(resolveFileViewer('a.markdown').viewer.render.widget).toBe('markdown_preview')
    for (const name of ['v.mp4', 'v.webm', 'a.mp3', 'a.flac']) {
      expect(resolveFileViewer(name).viewer.render.widget).toBe('media_preview')
    }
    expect(resolveFileViewer('r.html').viewer.render.widget).toBe('html_preview')
  })
})
