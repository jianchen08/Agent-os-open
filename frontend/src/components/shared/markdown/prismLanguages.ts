/**
 * PrismLight 按需语言注册 + 语言解析（单一真值源）
 *
 * 全量 `import { Prism }`（react-syntax-highlighter 主入口）携带 ~300 语言语法树，
 * 是启动 JS 堆的大头；这里改用 PrismLight 只注册下方 LANGUAGE_MODULES 列出的语言，
 * 代码块/文件预览/编辑器三处高亮统一从本模块取组件与语言映射。
 *
 * 契约：
 * - EXTENSION_TO_LANGUAGE 是「文件扩展名 → Prism 语言名」的唯一真值源，
 *   FilePreview 与 CodeEditor 共用，禁止在消费方复制表。
 * - PrismLight.registerLanguage(name, lang) 的首参被实现忽略（refractor.register
 *   按 lang.displayName 注册），别名经 SyntaxHighlighter.alias 显式注册；
 *   registeredNames 集合与 alias 调用同源填充，是 resolveHighlightLanguage
 *   守卫的唯一查询面。
 * - resolveHighlightLanguage 对未注册/空/纯文本语言统一回落 'text'
 *   （PrismLight 对未注册语言会抛错走 catch 渲染纯文本；显式回落避免每次渲染
 *   抛错开销与控制台噪声）。未注册语言的语义 = 无高亮纯文本，代码本体完整可复制。
 */

import PrismLight from 'react-syntax-highlighter/dist/esm/prism-light'
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash'
import batch from 'react-syntax-highlighter/dist/esm/languages/prism/batch'
import c from 'react-syntax-highlighter/dist/esm/languages/prism/c'
import cmake from 'react-syntax-highlighter/dist/esm/languages/prism/cmake'
import cpp from 'react-syntax-highlighter/dist/esm/languages/prism/cpp'
import csharp from 'react-syntax-highlighter/dist/esm/languages/prism/csharp'
import css from 'react-syntax-highlighter/dist/esm/languages/prism/css'
import dart from 'react-syntax-highlighter/dist/esm/languages/prism/dart'
import docker from 'react-syntax-highlighter/dist/esm/languages/prism/docker'
import go from 'react-syntax-highlighter/dist/esm/languages/prism/go'
import graphql from 'react-syntax-highlighter/dist/esm/languages/prism/graphql'
import groovy from 'react-syntax-highlighter/dist/esm/languages/prism/groovy'
import ini from 'react-syntax-highlighter/dist/esm/languages/prism/ini'
import java from 'react-syntax-highlighter/dist/esm/languages/prism/java'
import javascript from 'react-syntax-highlighter/dist/esm/languages/prism/javascript'
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json'
import jsx from 'react-syntax-highlighter/dist/esm/languages/prism/jsx'
import kotlin from 'react-syntax-highlighter/dist/esm/languages/prism/kotlin'
import less from 'react-syntax-highlighter/dist/esm/languages/prism/less'
import lua from 'react-syntax-highlighter/dist/esm/languages/prism/lua'
import makefile from 'react-syntax-highlighter/dist/esm/languages/prism/makefile'
import markdown from 'react-syntax-highlighter/dist/esm/languages/prism/markdown'
import markup from 'react-syntax-highlighter/dist/esm/languages/prism/markup'
import perl from 'react-syntax-highlighter/dist/esm/languages/prism/perl'
import php from 'react-syntax-highlighter/dist/esm/languages/prism/php'
import powershell from 'react-syntax-highlighter/dist/esm/languages/prism/powershell'
import properties from 'react-syntax-highlighter/dist/esm/languages/prism/properties'
import protobuf from 'react-syntax-highlighter/dist/esm/languages/prism/protobuf'
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python'
import r from 'react-syntax-highlighter/dist/esm/languages/prism/r'
import ruby from 'react-syntax-highlighter/dist/esm/languages/prism/ruby'
import rust from 'react-syntax-highlighter/dist/esm/languages/prism/rust'
import scss from 'react-syntax-highlighter/dist/esm/languages/prism/scss'
import sql from 'react-syntax-highlighter/dist/esm/languages/prism/sql'
import swift from 'react-syntax-highlighter/dist/esm/languages/prism/swift'
import toml from 'react-syntax-highlighter/dist/esm/languages/prism/toml'
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript'
import tsx from 'react-syntax-highlighter/dist/esm/languages/prism/tsx'
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml'
import zig from 'react-syntax-highlighter/dist/esm/languages/prism/zig'

/**
 * 注册的 Prism 语言模块
 *
 * - markup 覆盖 xml/html/svg（refractor markup 语法内置这些别名键）。
 * - 各语法文件自带依赖链（如 tsx 内部注册 jsx/markup），无需手工排序。
 */
const LANGUAGE_MODULES: Record<string, unknown> = {
  json,
  yaml,
  toml,
  markup,
  markdown,
  bash,
  properties,
  ini,
  graphql,
  protobuf,
  docker,
  makefile,
  groovy,
  cmake,
  python,
  javascript,
  jsx,
  typescript,
  tsx,
  css,
  scss,
  less,
  rust,
  go,
  java,
  kotlin,
  c,
  cpp,
  csharp,
  ruby,
  php,
  swift,
  batch,
  powershell,
  sql,
  r,
  lua,
  perl,
  dart,
  zig,
}

/** 常见代码围栏别名（refractor 语法元数据未声明、但 LLM 围栏常用的名字） */
const EXTRA_ALIASES: Record<string, string[]> = {
  go: ['golang'],
  cpp: ['c++'],
  csharp: ['c#'],
  bash: ['zsh'],
  powershell: ['ps', 'ps1'],
  batch: ['bat', 'cmd'],
  makefile: ['make', 'mf'],
  protobuf: ['proto'],
  graphql: ['gql'],
}

/** 已注册语言名（displayName + 全部别名），resolveHighlightLanguage 的查询面 */
const registeredNames = new Set<string>()

for (const [name, lang] of Object.entries(LANGUAGE_MODULES)) {
  PrismLight.registerLanguage(name, lang)
  registeredNames.add(name)
  const aliases: unknown = (lang as { aliases?: unknown }).aliases
  if (Array.isArray(aliases) && aliases.length > 0) {
    PrismLight.alias(name, aliases as string[])
    for (const alias of aliases) registeredNames.add(String(alias))
  }
}
for (const [name, aliases] of Object.entries(EXTRA_ALIASES)) {
  PrismLight.alias(name, aliases)
  for (const alias of aliases) registeredNames.add(alias)
}

/** 配置完语言注册的按需高亮组件（三处高亮消费方的唯一入口） */
export { PrismLight as PrismSyntaxHighlighter }

/**
 * 文件扩展名 → Prism 语言名（FilePreview / CodeEditor 共用真值源）
 *
 * 值必须命中 LANGUAGE_MODULES 注册名或 'text'（测试锁定）。
 */
export const EXTENSION_TO_LANGUAGE: Record<string, string> = {
  '.json': 'json',
  '.yaml': 'yaml',
  '.yml': 'yaml',
  '.toml': 'toml',
  '.xml': 'xml',
  '.html': 'html',
  '.htm': 'html',
  '.svg': 'xml',
  '.css': 'css',
  '.scss': 'scss',
  '.less': 'less',
  '.md': 'markdown',
  '.markdown': 'markdown',
  '.csv': 'text',
  '.tsv': 'text',
  '.txt': 'text',
  '.log': 'text',
  '.env': 'bash',
  '.properties': 'properties',
  '.ini': 'ini',
  '.cfg': 'ini',
  '.conf': 'ini',
  '.editorconfig': 'ini',
  '.graphql': 'graphql',
  '.gql': 'graphql',
  '.proto': 'protobuf',
  '.map': 'json',
  '.lock': 'text',
  '.gitignore': 'text',
  '.dockerignore': 'text',
  '.eslintrc': 'json',
  '.prettierrc': 'json',
  '.dockerfile': 'docker',
  '.makefile': 'makefile',
  '.gradle': 'groovy',
  '.cmake': 'cmake',
  '.py': 'python',
  '.js': 'javascript',
  '.jsx': 'jsx',
  '.ts': 'typescript',
  '.tsx': 'tsx',
  '.vue': 'html',
  '.svelte': 'html',
  '.rs': 'rust',
  '.go': 'go',
  '.java': 'java',
  '.kt': 'kotlin',
  '.c': 'c',
  '.cpp': 'cpp',
  '.h': 'c',
  '.hpp': 'cpp',
  '.cs': 'csharp',
  '.rb': 'ruby',
  '.php': 'php',
  '.swift': 'swift',
  '.sh': 'bash',
  '.bash': 'bash',
  '.bat': 'batch',
  '.ps1': 'powershell',
  '.sql': 'sql',
  '.r': 'r',
  '.lua': 'lua',
  '.pl': 'perl',
  '.dart': 'dart',
  '.zig': 'zig',
}

/**
 * 从文件名提取小写扩展名（点文件整名作扩展名，如 .env / .gitignore）
 *
 * @param fileName - 文件名或文件路径（/ 与 \ 均可）
 */
export function extractExtension(fileName: string): string {
  const lastSlash = Math.max(fileName.lastIndexOf('/'), fileName.lastIndexOf('\\'))
  const baseName = fileName.substring(lastSlash + 1)
  if (baseName.startsWith('.') && baseName.lastIndexOf('.') === 0) {
    return baseName.toLowerCase()
  }
  const dotIndex = baseName.lastIndexOf('.')
  if (dotIndex === -1) {
    return baseName.toLowerCase()
  }
  return baseName.substring(dotIndex).toLowerCase()
}

/**
 * 按文件名解析高亮语言（扩展名表 + 'text' 兜底）
 *
 * @param fileName - 文件名或文件路径
 */
export function getLanguageForFileName(fileName: string): string {
  return EXTENSION_TO_LANGUAGE[extractExtension(fileName)] ?? 'text'
}

/** 无高亮的纯文本围栏名（统一回落 'text'，不走注册表查询） */
const PLAINTEXT_LANGUAGES = new Set(['', 'text', 'txt', 'plaintext', 'plain'])

/**
 * 渲染前守卫：把任意来源（LLM 围栏 / artifact.language）的语言串解析为已注册语言名
 *
 * 未注册语言回落 'text' 并 console.debug 留痕——语义为无高亮纯文本，
 * 代码本体完整可复制（已接受的微差）。
 */
export function resolveHighlightLanguage(language?: string | null): string {
  const normalized = (language ?? '').trim().toLowerCase()
  if (PLAINTEXT_LANGUAGES.has(normalized)) return 'text'
  if (registeredNames.has(normalized)) return normalized
  console.debug(`[prismLanguages] 未注册高亮语言「${language}」，按纯文本渲染`)
  return 'text'
}
