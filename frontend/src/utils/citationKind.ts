/**
 * 引用卡片 kind 判定注册表（2026-11-05 R5 输入框引用卡片化）。
 *
 * 输入框引用卡片与消息流 ReferenceChip 是两条通道（通道语义不同，仅风格对齐），
 * kind 判定在此独立定义、不硬绑消息流渲染器：
 *  - 文件路径引用按扩展名分 code/doc/image；
 *  - 无扩展名/未知扩展名 → artifact（制品路径形态兜底）；
 *  - 消息引用 kind=message（入口处直接给定，不经本判定）。
 *
 * 扩展缝：新域入口经 registerCitationExtensionKind 登记新扩展名映射；全新 kind
 * 档（含视觉）在 CitationCardRow 的 kind 元数据表登记。
 */

/** 引用条目类型档（输入框卡片紧凑版；message=消息引用，artifact=制品兜底） */
export type CitationKind = 'code' | 'doc' | 'image' | 'artifact' | 'message'

/** 扩展名 → kind 映射（键=小写扩展名，不含点） */
const EXTENSION_KINDS = new Map<string, CitationKind>()

function registerExtensions(extensions: string[], kind: CitationKind): void {
  for (const ext of extensions) EXTENSION_KINDS.set(ext.toLowerCase(), kind)
}

registerExtensions(
  ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico', 'avif'],
  'image',
)
registerExtensions(
  ['md', 'markdown', 'txt', 'pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'csv', 'rtf', 'odt'],
  'doc',
)
registerExtensions(
  [
    'ts', 'tsx', 'js', 'jsx', 'mjs', 'cjs', 'py', 'rs', 'go', 'java', 'kt', 'swift',
    'c', 'h', 'cpp', 'hpp', 'cs', 'rb', 'php', 'sh', 'bash', 'zsh', 'ps1', 'bat',
    'sql', 'json', 'yaml', 'yml', 'toml', 'xml', 'html', 'css', 'scss', 'less',
    'vue', 'svelte', 'lua', 'dart', 'scala', 'gd', 'tscn', 'tres', 'godot', 'cfg', 'ini',
  ],
  'code',
)

/** 注册新扩展名映射（扩展缝；后注册覆盖先注册） */
export function registerCitationExtensionKind(extensions: string[], kind: CitationKind): void {
  registerExtensions(extensions, kind)
}

/**
 * 文件路径 → kind：取末段扩展名（大小写不敏感）查表；
 * 无扩展名（含隐藏文件与末段无点）/未知扩展名 → artifact。
 */
export function classifyCitationKind(sourcePath: string): CitationKind {
  const name = sourcePath.split(/[\\/]/).pop() ?? ''
  const dot = name.lastIndexOf('.')
  if (dot <= 0) return 'artifact'
  return EXTENSION_KINDS.get(name.slice(dot + 1).toLowerCase()) ?? 'artifact'
}
