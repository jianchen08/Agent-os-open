/** @feature FP-T12 前端适配（Prism 按需注册语言表） @vision V6 可即用 @ci frontend-test */
/**
 * prismLanguages 行为测试：扩展名映射（单一真值源）与渲染前语言守卫。
 *
 * 契约：
 * - EXTENSION_TO_LANGUAGE 为 FilePreview / CodeEditor 共用真值源，
 *   值只能命中已注册语言或 'text'（未注册值会让 PrismLight 每次渲染抛错走 catch）。
 * - resolveHighlightLanguage 对任意来源语言串输出「已注册名」或 'text'，永不透传未注册名。
 */

import { describe, expect, it, vi } from 'vitest'
import {
  EXTENSION_TO_LANGUAGE,
  extractExtension,
  getLanguageForFileName,
  resolveHighlightLanguage,
} from '../prismLanguages'

describe('getLanguageForFileName 扩展名映射', () => {
  it.each([
    // FilePreview 起源的键
    ['config/app.yaml', 'yaml'],
    ['config/app.yml', 'yaml'],
    ['data/notes.toml', 'toml'],
    ['proto/service.proto', 'protobuf'],
    // CodeEditor 起源的键
    ['src/main.py', 'python'],
    ['src/App.tsx', 'tsx'],
    ['lib/util.rs', 'rust'],
    // 合并表特有的交叉键（原两表各有、现同源）
    ['assets/schema.graphql', 'graphql'],
    ['assets/schema.gql', 'graphql'],
    ['.eslintrc', 'json'],
    ['Dockerfile2.dockerfile', 'docker'],
  ])('%s → %s', (filePath, expected) => {
    expect(getLanguageForFileName(filePath)).toBe(expected)
  })

  it('点文件整名作扩展名（.env → bash）', () => {
    expect(getLanguageForFileName('conf/.env')).toBe('bash')
    expect(extractExtension('conf/.env')).toBe('.env')
  })

  it('扩展名大小写不敏感（README.MD → markdown）', () => {
    expect(getLanguageForFileName('README.MD')).toBe('markdown')
  })

  it.each([
    ['docs/report.docx'],
    ['archive.tar.gz'],
    ['conf/Makefile'],
  ])('未知映射回落 text：%s', (filePath) => {
    expect(getLanguageForFileName(filePath)).toBe('text')
  })
})

describe('EXTENSION_TO_LANGUAGE 表值全部可高亮', () => {
  it('每个值要么是 text，要么命中已注册语言（守卫原样透传）', () => {
    const values = [...new Set(Object.values(EXTENSION_TO_LANGUAGE))]
    expect(values.length).toBeGreaterThan(20)
    for (const value of values) {
      if (value === 'text') continue
      expect(
        resolveHighlightLanguage(value),
        `表值 ${value} 应为已注册语言`,
      ).toBe(value)
    }
  })
})

describe('resolveHighlightLanguage 渲染前守卫', () => {
  it('已注册语言与别名原样放行', () => {
    expect(resolveHighlightLanguage('python')).toBe('python')
    expect(resolveHighlightLanguage('typescript')).toBe('typescript')
    // refractor 别名（如 golang）在 highlight 期由 alias 键解析，守卫放行
    expect(resolveHighlightLanguage('golang')).toBe('golang')
  })

  it('大小写与首尾空白归一化', () => {
    expect(resolveHighlightLanguage('  TypeScript\n')).toBe('typescript')
    expect(resolveHighlightLanguage('JSON')).toBe('json')
  })

  it.each([
    ['cobol'],
    ['brainfuck'],
    ['not-a-language'],
    ['<script>'],
  ])('未注册语言回落 text：%s', (language) => {
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {})
    expect(resolveHighlightLanguage(language)).toBe('text')
    debugSpy.mockRestore()
  })

  it.each([
    ['text'],
    ['txt'],
    ['plaintext'],
    ['plain'],
    [''],
    [undefined],
    [null],
  ])('纯文本/空值直接回落 text：%s', (language) => {
    expect(resolveHighlightLanguage(language as string | null | undefined)).toBe('text')
  })

  it('幂等性：对任意输入二次解析结果不变', () => {
    const inputs = ['python', 'cobol', 'TEXT', '', 'golang', 'shell']
    for (const input of inputs) {
      const once = resolveHighlightLanguage(input)
      expect(resolveHighlightLanguage(once)).toBe(once)
    }
  })
})
