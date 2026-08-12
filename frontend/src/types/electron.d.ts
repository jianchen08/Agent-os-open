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
 * 注入到 window 上的 electronAPI（子集）。
 *
 * 实际 preload 还暴露 onWindowInfo/getAppVersion/getPlatform/on 等，
 * 这里只声明窗口管理器消费的字段，避免与 Electron 类型耦合。
 */
export interface ElectronAPI {
  /** 窗口管理子 API */
  window: ElectronWindowAPI
}

/** 前端通过 window.electronAPI 访问（Electron 环境下存在，Web 下为 undefined） */
declare global {
  interface Window {
    electronAPI?: ElectronAPI
  }
}

export {}
