/**
 * Electron 渲染进程侧的 window.electronAPI 类型声明
 *
 * preload.ts 通过 contextBridge.exposeInMainWorld('electronAPI', ...) 注入。
 * 前端只声明实际消费的子集（窗口管理 + 平台/版本信息），
 * 其余字段（onWindowInfo 等）由 Electron 自身类型在需要时补充。
 *
 * 仅在 Electron 环境下 window.electronAPI 才存在；Web 构建下为 undefined，
 * ElectronWindowManager 据此降级到 WebWindowManager。
 */

/** 创建子窗口/悬浮窗的参数（对应 ipcMain.handle('window:open') 的 opts） */
export interface ElectronOpenWindowOptions {
  /** 窗口标识（前端传入，用于后续 close/focus） */
  id: string
  /** 加载的 URL（如 'http://localhost:5188/#/p/my-page' 深链） */
  url: string
  /** 窗口标题 */
  title?: string
  /** 窗口宽度，默认 320 */
  width?: number
  /** 窗口高度，默认 480 */
  height?: number
  /** 窗口左上角 X（不传则居中） */
  x?: number
  /** 窗口左上角 Y（不传则居中） */
  y?: number
  /** 是否有边框，默认 false（悬浮组件样式） */
  frame?: boolean
  /** 是否透明，默认 false */
  transparent?: boolean
  /** 是否置顶，默认 false */
  alwaysOnTop?: boolean
  /** 是否隐藏任务栏图标，默认 false */
  skipTaskbar?: boolean
}

/** window:open 的返回值 */
export interface ElectronOpenWindowResult {
  /** 复用的或新建的窗口 id */
  id: string
  /** 是否创建/聚焦成功 */
  success: boolean
}

/**
 * preload.ts 暴露的 window 子 API（ipcRenderer.invoke 封装）。
 *
 * 所有方法均返回 Promise（invoke 语义），不经过 preload 的 `on` 白名单
 * （白名单只作用于 ipcRenderer.on 监听通道）。
 */
export interface ElectronWindowAPI {
  /** 创建并返回子窗口；id 重复时聚焦已有窗口 */
  open(opts: ElectronOpenWindowOptions): Promise<ElectronOpenWindowResult>
  /** 关闭指定窗口并从注册表移除 */
  close(id: string): Promise<void>
  /** 聚焦指定窗口 */
  focus(id: string): Promise<void>
  /** 移动指定窗口 */
  move(id: string, pos: { x: number; y: number }): Promise<void>
  /** 调整指定窗口大小 */
  resize(id: string, size: { width: number; height: number }): Promise<void>
}

/**
 * preload.ts 暴露的主窗口自控子 API（自定义标题栏按钮）。
 *
 * ipcRenderer.invoke('window:self:*') 封装，主进程按 event.sender
 * 反查发起窗口，只作用于窗口自身。
 */
export interface ElectronWindowControlsAPI {
  /** 最小化 */
  minimize(): Promise<void>
  /** 最大化/还原切换；返回切换后的最大化状态 */
  toggleMaximize(): Promise<boolean>
  /** 关闭（主窗口 = 收进托盘，与原生 X 行为一致） */
  close(): Promise<void>
  /** 查询当前最大化状态 */
  isMaximized(): Promise<boolean>
  /** 监听最大化状态变化（双击拖拽区、Win+方向键等系统路径）；返回取消监听函数 */
  onMaximizedChange(callback: (maximized: boolean) => void): () => void
}

/**
 * preload.ts 暴露的系统通知子 API（ipcRenderer.invoke('notification:show') 封装）。
 *
 * 主进程用 Electron Notification 按宿主 OS 路由：Windows toast /
 * macOS 通知中心 / Linux libnotify。提示音仍由渲染进程 Web Audio 合成。
 */
export interface ElectronNotificationAPI {
  /** 弹出系统通知；返回是否成功弹出（宿主不支持/参数非法为 false） */
  show(opts: { title: string; body: string }): Promise<boolean>
}

/**
 * preload.ts 暴露的原生对话框子 API（ipcRenderer.invoke('dialog:pick-directory') 封装）。
 *
 * 主进程用 Electron dialog.showOpenDialog 弹出系统资源管理器目录选择。
 */
export interface ElectronDialogAPI {
  /** 选择目录；返回绝对路径，用户取消为 null */
  pickDirectory(): Promise<string | null>
}

/**
 * 注入到 window 上的 electronAPI（子集）。
 *
 * 实际 preload 还暴露 onWindowInfo/getAppVersion/getPlatform/on 等，
 * 这里只声明窗口管理器与 TitleBar 消费的字段，避免与 Electron 类型耦合。
 */
export interface ElectronAPI {
  /** 窗口管理子 API */
  window: ElectronWindowAPI
  /** 是否为子窗口/悬浮窗（TitleBar 仅主窗口渲染） */
  isChildWindow?: boolean
  /** 主窗口自控子 API（自定义标题栏按钮） */
  windowControls?: ElectronWindowControlsAPI
  /** 认证会话镜像子 API（refresh token 强杀耐久备份；Web/旧版壳下缺失，消费方判空） */
  authSession?: ElectronAuthSessionAPI
  /** 系统通知子 API（Electron 环境存在；Web/旧版壳下可能缺失，消费方须判空） */
  notification?: ElectronNotificationAPI
  /** 原生对话框子 API（Electron 环境存在；Web/旧版壳下缺失，消费方须判空） */
  dialog?: ElectronDialogAPI
}

/** 认证会话镜像子 API：refresh token 由主进程落盘，进程强杀不丢（自动登录跨重启） */
export interface ElectronAuthSessionAPI {
  save: (refreshToken: string | null) => Promise<boolean>
  load: () => Promise<string | null>
}

/** 前端通过 window.electronAPI 访问（Electron 环境下存在，Web 下为 undefined） */
declare global {
  interface Window {
    electronAPI?: ElectronAPI
  }
}

export {}
