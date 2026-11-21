/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */
/**
 * 工具类别派生测试（任务现场批 1）。
 *
 * 验收关注点（方案 §七 批 1 判据 3/4）：
 * - 三类行（bash / browser_navigate / browser_take_screenshot）类别互不相同
 * - 全量 LLM 工具**每条都能派生到某个类别**（无 undefined / 无异常）
 * - 非设备类工具（file_read）不误标成设备类别（区分度）
 * - 动作文案据实取参数，缺参不崩
 */
import { describe, expect, it } from 'vitest'
import {
  SCENE_CATEGORY_ORDER,
  deriveSceneCategory,
  describeBrowserAction,
  describeDesktopAction,
  describePhoneAction,
  describeSoftwareAction,
  describeToolAction,
} from '@/utils/toolCategory'

describe('deriveSceneCategory — 类别派生', () => {
  it('bash → terminal；浏览器两工具同属 browser 且与 terminal 互不相同', () => {
    expect(deriveSceneCategory('bash')).toBe('terminal')
    expect(deriveSceneCategory('browser_navigate')).toBe('browser')
    expect(deriveSceneCategory('browser_take_screenshot')).toBe('browser')
    // 三类行图标互不相同的判据：三个工具名派生出的集合至少含 2 个不同值
    const kinds = new Set([
      deriveSceneCategory('bash'),
      deriveSceneCategory('browser_navigate'),
      deriveSceneCategory('browser_take_screenshot'),
    ])
    expect(kinds.size).toBe(2)
    expect(kinds.has('terminal')).toBe(true)
    expect(kinds.has('browser')).toBe(true)
  })

  it('桌面八件 → desktop；App → software（软件与桌面分开）', () => {
    for (const tool of ['Snapshot', 'Screenshot', 'Click', 'Type', 'Scroll', 'Move', 'Shortcut', 'Wait']) {
      expect(deriveSceneCategory(tool)).toBe('desktop')
    }
    expect(deriveSceneCategory('App')).toBe('software')
  })

  it('phone_control → phone；builtin 七件 → file', () => {
    expect(deriveSceneCategory('phone_control')).toBe('phone')
    for (const tool of [
      'file_read',
      'file_write',
      'delete_file',
      'copy_file',
      'move_file',
      'create_directory',
      'enhanced_search',
    ]) {
      expect(deriveSceneCategory(tool)).toBe('file')
    }
  })

  it('区分度：非设备类工具不误标成设备类别', () => {
    expect(deriveSceneCategory('file_read')).not.toBe('browser')
    expect(deriveSceneCategory('file_read')).not.toBe('desktop')
    expect(deriveSceneCategory('file_read')).not.toBe('phone')
    expect(deriveSceneCategory('file_read')).not.toBe('software')
    expect(deriveSceneCategory('llm_chat')).toBe('other')
    expect(deriveSceneCategory('web_operate')).toBe('other')
  })

  it('全量已知工具（含未知/空名）都能派生到某个类别，无 undefined', () => {
    const allTools = [
      'bash', 'file_read', 'file_write', 'enhanced_search', 'create_directory', 'copy_file',
      'move_file', 'delete_file', 'browser_navigate', 'browser_snapshot', 'browser_click',
      'browser_type', 'browser_take_screenshot', 'browser_console_messages', 'phone_control',
      'Snapshot', 'Screenshot', 'Click', 'Type', 'Scroll', 'Move', 'Shortcut', 'Wait', 'App',
      'web_operate', 'github_ops', 'task_submit', 'human_interaction', 'eval_sleep',
      '完全未知的工具', '',
    ]
    for (const tool of allTools) {
      const category = deriveSceneCategory(tool)
      expect(typeof category).toBe('string')
      expect(SCENE_CATEGORY_ORDER).toContain(category)
    }
    expect(deriveSceneCategory('')).toBe('other')
    expect(deriveSceneCategory('完全未知的工具')).toBe('other')
  })
})

describe('describeXAction — 动作文案据实取参', () => {
  it('浏览器：navigate 带 url，type 带文本与目标（两组有区分度输入）', () => {
    expect(describeBrowserAction('browser_navigate', { url: 'https://example.com/a' })).toBe(
      '导航 https://example.com/a',
    )
    expect(describeBrowserAction('browser_navigate', {})).toBe('导航')
    expect(
      describeBrowserAction('browser_type', { target: '#login', text: 'user@example.com' }),
    ).toBe('输入 "user@example.com" → #login')
    expect(describeBrowserAction('browser_type', { text: '仅文本' })).toBe('输入 "仅文本"')
  })

  it('桌面：Click 优先 label，无 label 落坐标，缺参不崩', () => {
    expect(describeDesktopAction('Click', { label: '[12] 保存按钮' })).toBe('点击 [12] 保存按钮')
    expect(describeDesktopAction('Click', { x: 120, y: 340 })).toBe('点击 (120, 340)')
    expect(describeDesktopAction('Click', {})).toBe('点击')
    expect(describeDesktopAction('Snapshot', {})).toBe('窗口元素快照')
  })

  it('手机：click 含坐标、key 含 keycode 语义、input 含文本', () => {
    expect(describePhoneAction({ action: 'click', x: 120, y: 340 })).toBe('点击 (120, 340)')
    expect(describePhoneAction({ action: 'key', keycode: 4 })).toBe('返回(4)')
    expect(describePhoneAction({ action: 'key', keycode: 999 })).toBe('按键(999)')
    expect(describePhoneAction({ action: 'input', text: '你好' })).toBe('输入 "你好"')
    expect(describePhoneAction({ action: 'tree' })).toBe('读取元素树')
  })

  it('软件：App 带应用名', () => {
    expect(describeSoftwareAction({ name: '记事本' })).toBe('启动 记事本')
    expect(describeSoftwareAction({})).toBe('软件操作')
  })

  it('describeToolAction 对任意工具返回非空字符串（行文案不允许空）', () => {
    const cases: Array<[string, Record<string, unknown>]> = [
      ['bash', { command: 'ls -la' }],
      ['bash', {}],
      ['browser_navigate', { url: 'https://x.test' }],
      ['Click', { x: 1, y: 2 }],
      ['phone_control', { action: 'screenshot' }],
      ['App', { name: 'Excel' }],
      ['file_read', { path: 'a.md' }],
      ['完全未知的工具', {}],
    ]
    for (const [tool, args] of cases) {
      const text = describeToolAction(tool, args)
      expect(text.length).toBeGreaterThan(0)
    }
    expect(describeToolAction('bash', { command: 'ls -la' })).toBe('ls -la')
    expect(describeToolAction('file_read', { path: 'a.md' })).toBe('file_read')
  })
})

describe('describeXAction — 缺参/双键名/截断分支补缺（覆盖清偿）', () => {
  it('浏览器常量四件 + 未知回落；click 双键名与缺参', () => {
    expect(describeBrowserAction('browser_snapshot', {})).toBe('页面快照')
    expect(describeBrowserAction('browser_take_screenshot', {})).toBe('浏览器截图')
    expect(describeBrowserAction('browser_console_messages', {})).toBe('控制台消息')
    expect(describeBrowserAction('browser_unknown', {})).toBe('浏览器操作')
    expect(describeBrowserAction('browser_click', { target: '#submit' })).toBe('点击 #submit')
    expect(describeBrowserAction('browser_click', { element: '登录' })).toBe('点击 登录')
    expect(describeBrowserAction('browser_click', {})).toBe('点击')
  })

  it('browser_type 仅目标/双缺两态；超长文本单行截断', () => {
    expect(describeBrowserAction('browser_type', { target: '输入框' })).toBe('输入 → 输入框')
    expect(describeBrowserAction('browser_type', {})).toBe('输入')
    expect(describeBrowserAction('browser_type', { text: 'x'.repeat(80) })).toContain('…')
  })

  it('桌面：Type/Move/Shortcut/Scroll/Wait/未知 全分支；Click 的 element 键名', () => {
    expect(describeDesktopAction('Type', { text: 'hello' })).toBe('输入 "hello"')
    expect(describeDesktopAction('Type', {})).toBe('输入')
    expect(describeDesktopAction('Move', { x: 1, y: 2 })).toBe('移动 (1, 2)')
    expect(describeDesktopAction('Move', {})).toBe('移动')
    expect(describeDesktopAction('Shortcut', { keys: 'Ctrl+S' })).toBe('快捷键 Ctrl+S')
    expect(describeDesktopAction('Shortcut', { shortcut: 'Alt+Tab' })).toBe('快捷键 Alt+Tab')
    expect(describeDesktopAction('Shortcut', {})).toBe('快捷键')
    expect(describeDesktopAction('Scroll', {})).toBe('滚动')
    expect(describeDesktopAction('Wait', {})).toBe('等待')
    expect(describeDesktopAction('Resize', {})).toBe('桌面操作')
    expect(describeDesktopAction('Click', { element: '[3] 菜单' })).toBe('点击 [3] 菜单')
  })

  it('桌面真名 desktop_*：windows/scroll/drag/wait/sequence/web 常量与 window/invoke/read/text 带参缺参双态', () => {
    // 无参常量族（活工具面真名，现场页动作文案消费）
    expect(describeDesktopAction('desktop_windows', {})).toBe('列出桌面窗口')
    expect(describeDesktopAction('desktop_scroll', {})).toBe('滚动')
    expect(describeDesktopAction('desktop_drag', {})).toBe('拖拽')
    expect(describeDesktopAction('desktop_wait', {})).toBe('等待条件成立')
    expect(describeDesktopAction('desktop_sequence', {})).toBe('桌面动作序列')
    expect(describeDesktopAction('desktop_web', {})).toBe('网页操作')
    // 双态族：带参报已知值，缺参走泛化词（不编造目标）
    expect(describeDesktopAction('desktop_click', { name: '[7] 确认' })).toBe('点击 [7] 确认')
    expect(describeDesktopAction('desktop_click', { x: 30, y: 90 })).toBe('点击 (30, 90)')
    expect(describeDesktopAction('desktop_click', {})).toBe('点击')
    expect(describeDesktopAction('desktop_window', { action: 'minimize' })).toBe('窗口操作 minimize')
    expect(describeDesktopAction('desktop_window', {})).toBe('窗口操作')
    expect(describeDesktopAction('desktop_invoke', { ref: '[2] 菜单' })).toBe('触发控件 [2] 菜单')
    expect(describeDesktopAction('desktop_invoke', {})).toBe('触发控件')
    expect(describeDesktopAction('desktop_read', { window: '记事本' })).toBe('OCR 读窗口 记事本')
    expect(describeDesktopAction('desktop_read', {})).toBe('OCR 读窗口')
    expect(describeDesktopAction('desktop_text', { window: '设置' })).toBe('读窗口文本 设置')
    expect(describeDesktopAction('desktop_text', {})).toBe('读窗口文本')
    // 抽象旧名 Screenshot（历史落库消息兼容）
    expect(describeDesktopAction('Screenshot', {})).toBe('桌面截图')
  })

  it('手机：无 action 回落；字符串数字坐标也认；超长输入截断；keycode 66', () => {
    expect(describePhoneAction({})).toBe('手机操作')
    expect(describePhoneAction({ action: 'unknown' })).toBe('手机操作')
    expect(describePhoneAction({ action: 'click', x: '12', y: '34' })).toBe('点击 (12, 34)')
    expect(describePhoneAction({ action: 'click', x: 1 })).toBe('点击')
    expect(describePhoneAction({ action: 'input', text: 'a'.repeat(80) })).toContain('…')
    expect(describePhoneAction({ action: 'input' })).toBe('输入')
    expect(describePhoneAction({ action: 'key', keycode: 66 })).toBe('回车(66)')
    expect(describePhoneAction({ action: 'key' })).toBe('按键')
  })
})

describe('活工具面真名（desktop_*）— 设备/软件类别不再落 other', () => {
  /**
   * 回归锚：现场页曾按 Snapshot/Click/App 这类抽象名匹配，而活注册表的 MCP 外部
   * 工具名是 `desktop_*`（`desktop_capture`/`desktop_launch`/`desktop_windows` …），
   * 于是桌面/软件条目全部落 other、现场页永远筛不到设备类别。
   */
  it('desktop_* → desktop（除 desktop_launch 归 software）', () => {
    const desktopTools = [
      'desktop_capture',
      'desktop_look',
      'desktop_windows',
      'desktop_tree',
      'desktop_click',
      'desktop_type',
      'desktop_key',
      'desktop_scroll',
      'desktop_drag',
      'desktop_window',
      'desktop_invoke',
      'desktop_read',
      'desktop_text',
      'desktop_wait',
      'desktop_sequence',
      'desktop_web',
      'desktop_course',
      'desktop_doctor',
      'desktop_log',
      'desktop_steer',
    ]
    for (const tool of desktopTools) {
      expect(deriveSceneCategory(tool)).toBe('desktop')
    }
    expect(deriveSceneCategory('desktop_launch')).toBe('software')
  })

  it('前缀兜底：未枚举的新 desktop_ 工具同样归桌面（抗漂移）', () => {
    expect(deriveSceneCategory('desktop_something_new')).toBe('desktop')
  })

  it('设备三类别两两互不相同（筛选条按类别分流的判据）', () => {
    const kinds = new Set([
      deriveSceneCategory('desktop_capture'),
      deriveSceneCategory('desktop_launch'),
      deriveSceneCategory('phone_control'),
    ])
    expect(kinds.size).toBe(3)
  })

  it('桌面动作文案取真实入参：window/desktop/ref/key/text', () => {
    expect(describeDesktopAction('desktop_capture', { window: '记事本' })).toBe('窗口截图 记事本')
    expect(describeDesktopAction('desktop_capture', { desktop: true })).toBe('桌面截图（全屏）')
    expect(describeDesktopAction('desktop_capture', {})).toBe('桌面截图')
    expect(describeDesktopAction('desktop_click', { ref: '12', window: 'Notepad' })).toBe('点击 12')
    expect(describeDesktopAction('desktop_click', { x: 10, y: 20 })).toBe('点击 (10, 20)')
    expect(describeDesktopAction('desktop_key', { key: 'ctrl+a' })).toBe('按键 ctrl+a')
    expect(describeDesktopAction('desktop_type', { text: '你好' })).toBe('输入 "你好"')
    expect(describeDesktopAction('desktop_tree', { window: 'Notepad' })).toBe('控件树 Notepad')
    expect(describeDesktopAction('desktop_look', {})).toBe('屏幕现状（前台窗口 + 可点控件）')
  })

  it('软件动作文案取 launch 三入参（app/file/url）', () => {
    expect(describeSoftwareAction({ app: 'notepad' })).toBe('启动 notepad')
    expect(describeSoftwareAction({ url: 'https://example.com' })).toBe('启动 https://example.com')
    expect(describeSoftwareAction({ file: 'C:/a.docx' })).toBe('启动 C:/a.docx')
    expect(describeSoftwareAction({})).toBe('软件操作')
  })
})
