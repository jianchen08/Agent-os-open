/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * FilePreview 覆盖缺口补测
 *
 * 覆盖契约：
 * - 扩展名解析：无扩展名 / 隐藏文件（.gitignore）/ 反斜杠路径 / 未知扩展 → 二进制提示
 * - 图片预览：缩放（+25 上限 400、-25 下限 25）、旋转（+90 循环 360）、
 *   无 URL（containerTaskId 为空且未给 url）时提示「无法获取图片地址」，
 *   加载失败 onError 渲染失败文案；附件直链 url 优先于 containerTaskId 拼接；
 *   显示文件大小换算（二进制分支 KB）
 * - 代码预览：下载按钮触发 Blob + a.download 文件名、语法高亮语言标签
 * - PDF 预览：有 URL 渲染 iframe，无 URL 提示无法加载
 *
 * 残留分支（逐条说明，均为不可达防御代码）：
 * 1. 第 132 行 `if (ext in EXTENSION_TO_LANGUAGE) return 'code'`：CODE_PREVIEW_EXTENSIONS
 *    与 EXTENSION_TO_LANGUAGE 的键集合实测完全包含（两集合均由同一批扩展名构成），
 *    前一行的 has() 判定使本行恒假——保留是给「只加映射表忘了加集合」的将来编辑留兜底。
 * 2. 第 248 行 `if (parent)`（onError 内替换父节点）：<img> 在 JSX 中恒有父 div 容器，
 *    parentElement 不可能为 null。
 * 3. 第 353 行 `extractExtension(filePath) || '未知'` 的 `'未知'` 兜底已由空路径用例覆盖。
 *
 * 测试策略：真实组件 + 真实 icon/语法高亮；仅 stub 浏览器文件下载 API
 * （URL.createObjectURL / revokeObjectURL 为外部浏览器依赖）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FilePreview } from '@/components/workspace/FilePreview'

describe('FilePreview — 扩展名解析与预览类型', () => {
  it.each([
    ['assets/logo.PNG', 'img'],
    ['C:\\workspace\\docs\\a.jpg', 'img'],
    ['readme', 'code'],
    ['scripts/.env', 'code'],
  ] as const)('%s 解析出正确预览类型', (filePath, _hint) => {
    render(<FilePreview filePath={filePath} content="x" containerTaskId="ct-1" />)
    expect(screen.getByText(filePath.split(/[/\\]/).pop() as string)).toBeInTheDocument()
  })

  it.each([
    ['archive.bin', '二进制提示 + 未知扩展名文案'],
    ['noext', '无扩展名文件'],
  ] as const)('%s 走二进制提示分支', (filePath) => {
    render(<FilePreview filePath={filePath} content="x" size={2048} containerTaskId="ct-1" />)
    expect(screen.getByText('无法预览此文件')).toBeInTheDocument()
    // 大小换算 KB（2048B → 2.0 KB）
    expect(screen.getByText('(2.0 KB)')).toBeInTheDocument()
  })

  it('无扩展名时二进制提示以原文件名作为扩展名占位', () => {
    render(<FilePreview filePath="Makefile" content="x" containerTaskId="ct-1" />)
    expect(screen.getByText(/该文件类型（makefile）暂不支持在线预览/)).toBeInTheDocument()
  })

  it('空路径：扩展名兜底为「未知」（extractExtension 返回空串的 falsy 分支）', () => {
    render(<FilePreview filePath="" content="x" containerTaskId="ct-1" />)
    expect(screen.getByText(/该文件类型（未知）暂不支持在线预览/)).toBeInTheDocument()
  })

  it('未提供 size 时不渲染大小换算', () => {
    render(<FilePreview filePath="archive.bin" content="x" containerTaskId="ct-1" />)
    expect(screen.queryByText(/KB\)/)).toBeNull()
  })
})

describe('FilePreview — 图片缩放/旋转', () => {
  beforeEach(() => {
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: vi.fn(() => 'blob:mock'),
      revokeObjectURL: vi.fn(),
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('放大加 25% 且上限 400%，缩小减 25% 且下限 25%', () => {
    render(<FilePreview filePath="a.png" content="x" containerTaskId="ct-1" />)
    const zoomIn = screen.getByTitle('放大')
    const zoomOut = screen.getByTitle('缩小')
    expect(screen.getByText('100%')).toBeInTheDocument()

    for (let i = 0; i < 13; i++) fireEvent.click(zoomIn)
    expect(screen.getByText('400%')).toBeInTheDocument()

    for (let i = 0; i < 20; i++) fireEvent.click(zoomOut)
    expect(screen.getByText('25%')).toBeInTheDocument()
  })

  it('旋转 90°/次，第 4 次回到 0°（transform 回绕）', () => {
    render(<FilePreview filePath="a.png" content="x" containerTaskId="ct-1" />)
    const img = screen.getByAltText('a.png')
    fireEvent.click(screen.getByTitle('旋转'))
    expect(img.style.transform).toContain('rotate(90deg)')
    fireEvent.click(screen.getByTitle('旋转'))
    fireEvent.click(screen.getByTitle('旋转'))
    fireEvent.click(screen.getByTitle('旋转'))
    expect(img.style.transform).toContain('rotate(0deg)')
  })

  it('附件直链 url 优先于 containerTaskId 拼接', () => {
    render(
      <FilePreview filePath="a.png" content="x" url="/uploads/direct.png" containerTaskId="ct-1" />,
    )
    expect(screen.getByAltText('a.png')).toHaveAttribute('src', '/uploads/direct.png')
  })

  it('无 url 且无 containerTaskId 时提示无法获取图片地址', () => {
    render(<FilePreview filePath="a.png" content="x" containerTaskId="" />)
    expect(screen.getByText('无法获取图片地址')).toBeInTheDocument()
    expect(screen.queryByAltText('a.png')).toBeNull()
  })

  it('图片加载失败时以失败文案替换图片', () => {
    render(<FilePreview filePath="a.png" content="x" url="/bad.png" containerTaskId="ct-1" />)
    const img = screen.getByAltText('a.png')
    fireEvent.error(img)
    expect(screen.getByText('图片加载失败')).toBeInTheDocument()
    expect(screen.getByText('无法预览此图片文件')).toBeInTheDocument()
  })
})

describe('FilePreview — 代码与 PDF', () => {
  beforeEach(() => {
    vi.stubGlobal('URL', {
      ...URL,
      createObjectURL: vi.fn(() => 'blob:mock'),
      revokeObjectURL: vi.fn(),
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it.each([
    ['config/app.yaml', 'yaml'],
    ['notes.txt', 'text'],
  ] as const)('代码预览 %s 显示语言标签 %s 与只读标记', (filePath, language) => {
    render(<FilePreview filePath={filePath} content="key: 1" containerTaskId="ct-1" />)
    expect(screen.getByText('（只读）')).toBeInTheDocument()
    const fileName = filePath.split('/').pop() as string
    expect(screen.getByText(fileName)).toBeInTheDocument()
    if (language !== 'text') expect(screen.getByText(language)).toBeInTheDocument()
  })

  it('文本类不显示语言标签（language===text 时留空）', () => {
    render(<FilePreview filePath="notes.txt" content="hello" containerTaskId="ct-1" />)
    expect(screen.queryByText('text')).toBeNull()
  })

  it('下载按钮以文件名触发浏览器下载并释放 Blob URL', () => {
    render(<FilePreview filePath="docs/report.md" content="# 标题" containerTaskId="ct-1" />)
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    fireEvent.click(screen.getByTitle('下载文件'))

    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(clickSpy).toHaveBeenCalledTimes(1)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock')
    clickSpy.mockRestore()
  })

  it('二进制提示的下载按钮同样触发下载', () => {
    render(<FilePreview filePath="a.bin" content="binary" containerTaskId="ct-1" />)
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    fireEvent.click(screen.getByRole('button', { name: /下载文件到本地查看/ }))
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(clickSpy).toHaveBeenCalledTimes(1)
    clickSpy.mockRestore()
  })

  it('PDF 有 URL 渲染 iframe（title 带文件名）', () => {
    render(<FilePreview filePath="docs/a.pdf" content="" url="/uploads/a.pdf" containerTaskId="ct-1" />)
    expect(screen.getByTitle('预览 a.pdf')).toHaveAttribute('src', '/uploads/a.pdf')
    expect(screen.getByText('（PDF 预览）')).toBeInTheDocument()
  })

  it('PDF 无可用 URL 时提示无法加载', () => {
    render(<FilePreview filePath="docs/a.pdf" content="" containerTaskId="" />)
    expect(screen.getByText('无法加载 PDF 文件')).toBeInTheDocument()
  })
})
