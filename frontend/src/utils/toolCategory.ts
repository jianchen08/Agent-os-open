/**
 * 工具类别派生（任务现场时间轴用，纯函数、零后端改动）
 *
 * 现场页要把一次任务的运行现场按「命令 / 浏览器 / 桌面 / 手机 / 软件 / 文件 / 其它」
 * 分门别类呈现。内核 `ToolDescriptor` 不带类别语义（`kernel/crates/core/src/types.rs`
 * 只有 name/description/schema），故类别在本模块按工具名前缀派生：
 *
 * - 浏览器：`browser_*`（browser_tool 插件六工具）
 * - 桌面：`desktop_*` 桌面通道（capture/look/tree/click/type/key/… 真实工具面）
 * - 手机：`phone_control`（单工具五动作，动作在 args.action）
 * - 软件：`desktop_launch`（启动应用/打开文件/打开 URL，与桌面同通道但语义独立）
 * - 命令：`bash`（终端同口径卡片）
 * - 文件：builtin_tools 七件（file_read/file_write/delete_file/...）
 * - 其它：其余全部（LLM、检索、通道…）
 *
 * 工具名真值 = 活注册表的 MCP 外部工具名（`desktop_capture` / `desktop_launch` /
 * `desktop_windows` …），不是抽象语义名。历史上本模块曾按 `Snapshot`/`Click`/`App`
 * 这类抽象名匹配，实测活工具面里不存在这些名字 → 桌面/软件条目全部落 `other`、
 * 现场页永远看不到设备类别（本次修复点）。旧名保留映射只为兼容历史落库消息。
 *
 * 未知工具一律落 `other`，**不抛错、不返回 undefined**——现场页按类别筛选，
 * 无类别的条目会从所有筛选项里消失（等于静默丢现场）。
 *
 * 依据：docs/working/9b59e3318956_工作区任务现场方案.md §3.3.0
 */

/** 现场类别（时间轴筛选维度 + 行图标选择键） */
export type SceneToolCategory =
  | 'terminal'
  | 'browser'
  | 'desktop'
  | 'phone'
  | 'software'
  | 'file'
  | 'other'

/** 类别显示文案（筛选按钮/分组标题） */
export const SCENE_CATEGORY_LABELS: Record<SceneToolCategory, string> = {
  terminal: '命令',
  browser: '浏览器',
  desktop: '桌面',
  phone: '手机',
  software: '软件',
  file: '文件',
  other: '其它',
}

/** 类别渲染顺序（筛选条与分组按此序） */
export const SCENE_CATEGORY_ORDER: SceneToolCategory[] = [
  'terminal',
  'browser',
  'desktop',
  'phone',
  'software',
  'file',
  'other',
]

/**
 * 桌面通道工具名（活注册表的 `desktop_*` 工具面）。
 *
 * `desktop_launch` 单独归软件类别（启动应用/打开文件/打开 URL），其余全部归桌面。
 * 全量清单取自活工具面实测（`desktop_capture` … `desktop_web` 共 21 件）；新工具
 * 按 `desktop_` 前缀兜底，不在集合里也算桌面（前缀比枚举更抗漂移）。
 */
const DESKTOP_TOOL_PREFIX = 'desktop_'

/** 启动软件/打开文件/URL 的工具（软件类别单列） */
const SOFTWARE_TOOLS = new Set(['desktop_launch', 'App'])

/** 历史落库消息里的抽象桌面名（活工具面已无此名，仅兼容旧数据） */
const LEGACY_DESKTOP_TOOLS = new Set([
  'Snapshot',
  'Screenshot',
  'Click',
  'Type',
  'Scroll',
  'Move',
  'Shortcut',
  'Wait',
])

/** builtin_tools 七件（文件域） */
const FILE_TOOLS = new Set([
  'file_read',
  'file_write',
  'delete_file',
  'copy_file',
  'move_file',
  'create_directory',
  'enhanced_search',
])

/** 工具名 → 现场类别（见文件头映射表；未知工具落 other） */
export function deriveSceneCategory(toolName: string): SceneToolCategory {
  const name = toolName.trim()
  if (name === '') return 'other'
  if (name === 'bash') return 'terminal'
  if (name.startsWith('browser_')) return 'browser'
  if (name === 'phone_control') return 'phone'
  if (SOFTWARE_TOOLS.has(name)) return 'software'
  if (name.startsWith(DESKTOP_TOOL_PREFIX)) return 'desktop'
  if (LEGACY_DESKTOP_TOOLS.has(name)) return 'desktop'
  if (FILE_TOOLS.has(name)) return 'file'
  return 'other'
}

/** 手机动作枚举（phone_control 的 args.action） */
export type PhoneAction = 'tree' | 'click' | 'input' | 'key' | 'screenshot'

/**
 * 手机动作 → 一行文案。
 *
 * keycode 语义按 Android 常用映射给出可读名（4=返回、3=Home、66=回车…），
 * 未收录的 keycode 原样显示数字（不猜测语义）。
 */
export function describePhoneAction(args: Record<string, unknown>): string {
  const action = typeof args.action === 'string' ? (args.action as PhoneAction) : ''
  switch (action) {
    case 'tree':
      return '读取元素树'
    case 'screenshot':
      return '手机截图'
    case 'click': {
      const x = numOr(args.x)
      const y = numOr(args.y)
      return x !== undefined && y !== undefined ? `点击 (${x}, ${y})` : '点击'
    }
    case 'input': {
      const text = typeof args.text === 'string' ? args.text : ''
      return text ? `输入 "${clip(text)}"` : '输入'
    }
    case 'key': {
      const code = numOr(args.keycode)
      if (code === undefined) return '按键'
      const label = ANDROID_KEY_LABELS[code]
      return label ? `${label}(${code})` : `按键(${code})`
    }
    default:
      return '手机操作'
  }
}

/** Android keycode → 可读名（仅收录仓内文案会用到的常用值） */
const ANDROID_KEY_LABELS: Record<number, string> = {
  3: 'Home',
  4: '返回',
  24: '音量+',
  25: '音量-',
  26: '电源',
  66: '回车',
  67: '退格',
  82: '菜单',
}

/** dumpsys 类长文本单行化（超长截断，避免撑破行高） */
const INLINE_MAX_LEN = 60

function clip(text: string): string {
  const oneLine = text.replace(/\s+/g, ' ').trim()
  return oneLine.length > INLINE_MAX_LEN ? `${oneLine.slice(0, INLINE_MAX_LEN)}…` : oneLine
}

function numOr(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return undefined
}

/** 浏览器动作 → 一行文案（浏览器插件六工具） */
export function describeBrowserAction(toolName: string, args: Record<string, unknown>): string {
  switch (toolName) {
    case 'browser_navigate': {
      const url = typeof args.url === 'string' ? args.url : ''
      return url ? `导航 ${clip(url)}` : '导航'
    }
    case 'browser_snapshot':
      return '页面快照'
    case 'browser_click': {
      const target = strOr(args.target) ?? strOr(args.element)
      return target ? `点击 ${clip(target)}` : '点击'
    }
    case 'browser_type': {
      const target = strOr(args.target) ?? strOr(args.element)
      const text = strOr(args.text)
      if (target && text) return `输入 "${clip(text)}" → ${clip(target)}`
      if (text) return `输入 "${clip(text)}"`
      return target ? `输入 → ${clip(target)}` : '输入'
    }
    case 'browser_take_screenshot':
      return '浏览器截图'
    case 'browser_console_messages':
      return '控制台消息'
    default:
      return '浏览器操作'
  }
}

/**
 * 桌面动作 → 一行文案（桌面通道工具面 `desktop_*`）。
 *
 * 参数名取各工具的真实入参：窗口用 `window`（hwnd 或标题片段），控件用 `ref`/`name`，
 * 坐标为客户端像素 x/y。文案只报已知值，缺值走泛化词（不编造目标）。
 * 抽象旧名（Snapshot/Click/App）保留分支只为兼容历史落库消息。
 */
export function describeDesktopAction(toolName: string, args: Record<string, unknown>): string {
  const win = strOr(args.window)
  const target = strOr(args.name) ?? strOr(args.ref) ?? strOr(args.label) ?? strOr(args.element)
  switch (toolName) {
    case 'desktop_capture':
      if (args.desktop === true) return '桌面截图（全屏）'
      return win ? `窗口截图 ${clip(win)}` : '桌面截图'
    case 'desktop_look':
      return '屏幕现状（前台窗口 + 可点控件）'
    case 'desktop_windows':
      return '列出桌面窗口'
    case 'desktop_tree':
      return win ? `控件树 ${clip(win)}` : '控件树'
    case 'desktop_click': {
      const x = numOr(args.x)
      const y = numOr(args.y)
      if (target) return `点击 ${clip(target)}`
      if (x !== undefined && y !== undefined) return `点击 (${x}, ${y})`
      return '点击'
    }
    case 'desktop_type': {
      const text = strOr(args.text)
      return text ? `输入 "${clip(text)}"` : '输入'
    }
    case 'desktop_key': {
      const key = strOr(args.key) ?? strOr(args.keys)
      return key ? `按键 ${clip(key)}` : '按键'
    }
    case 'desktop_scroll':
      return '滚动'
    case 'desktop_drag':
      return '拖拽'
    case 'desktop_window': {
      const op = strOr(args.action) ?? strOr(args.op)
      return op ? `窗口操作 ${clip(op)}` : '窗口操作'
    }
    case 'desktop_invoke':
      return target ? `触发控件 ${clip(target)}` : '触发控件'
    case 'desktop_read':
      return win ? `OCR 读窗口 ${clip(win)}` : 'OCR 读窗口'
    case 'desktop_text':
      return win ? `读窗口文本 ${clip(win)}` : '读窗口文本'
    case 'desktop_wait':
      return '等待条件成立'
    case 'desktop_sequence':
      return '桌面动作序列'
    case 'desktop_web':
      return '网页操作'
    // 旧抽象名（历史消息兼容）
    case 'Snapshot':
      return '窗口元素快照'
    case 'Screenshot':
      return '桌面截图'
    case 'Click': {
      if (target) return `点击 ${clip(target)}`
      const x = numOr(args.x)
      const y = numOr(args.y)
      return x !== undefined && y !== undefined ? `点击 (${x}, ${y})` : '点击'
    }
    case 'Type': {
      const text = strOr(args.text)
      return text ? `输入 "${clip(text)}"` : '输入'
    }
    case 'Scroll':
      return '滚动'
    case 'Move': {
      const x = numOr(args.x)
      const y = numOr(args.y)
      return x !== undefined && y !== undefined ? `移动 (${x}, ${y})` : '移动'
    }
    case 'Shortcut': {
      const keys = strOr(args.keys) ?? strOr(args.shortcut)
      return keys ? `快捷键 ${clip(keys)}` : '快捷键'
    }
    case 'Wait':
      return '等待'
    default:
      return '桌面操作'
  }
}

/**
 * 软件动作 → 一行文案（`desktop_launch`：启动应用 / 打开文件 / 打开 URL）。
 *
 * 三个入参互斥（app / file / url），据实显示启动目标；全缺走泛化词。
 */
export function describeSoftwareAction(args: Record<string, unknown>): string {
  const launchTarget =
    strOr(args.app) ?? strOr(args.file) ?? strOr(args.url) ?? strOr(args.name) ?? strOr(args.application)
  const mode = strOr(args.action) ?? strOr(args.mode)
  if (launchTarget) return mode === 'switch' ? `切换 ${clip(launchTarget)}` : `启动 ${clip(launchTarget)}`
  return '软件操作'
}

function strOr(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value : undefined
}

/**
 * 任一工具 → 时间轴一行文案（类别内部先分派器，未知类别回落工具名）。
 *
 * 明确返回非空字符串：现场页行文案不允许空（空行等于看不到做了什么）。
 */
export function describeToolAction(toolName: string, args: Record<string, unknown>): string {
  const category = deriveSceneCategory(toolName)
  switch (category) {
    case 'browser':
      return describeBrowserAction(toolName, args)
    case 'desktop':
      return describeDesktopAction(toolName, args)
    case 'phone':
      return describePhoneAction(args)
    case 'software':
      return describeSoftwareAction(args)
    case 'terminal': {
      const command = strOr(args.command)
      return command ? clip(command) : '命令'
    }
    default:
      return toolName
  }
}
