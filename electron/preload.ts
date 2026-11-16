/**
 * Electron 预加载脚本。
 *
 * 通过 contextBridge 安全地将 Electron API 暴露给渲染进程，
 * 提供应用版本查询等基础能力。
 */

import { contextBridge, ipcRenderer } from "electron";

/** 通过 contextBridge 暴露给渲染进程的 API 接口 */
/** WS 中继 URL 请求参数（目标基址 + 握手票据 + 协议版本 + 补漏游标） */
export interface WsRelayUrlParams {
  target: string;
  ticket: string;
  version?: string;
  last_sequence?: number;
}

export interface ElectronAPI {
  /**
   * 获取应用版本号。
   *
   * @returns 版本号字符串
   */
  getAppVersion(): string;

  /**
   * 获取当前运行平台。
   *
   * @returns 平台标识字符串（win32 / darwin / linux）
   */
  getPlatform(): string;

  /**
   * 窗口管理子 API（P2/P3 多窗口基础设施）。
   *
   * 所有方法均通过 ipcRenderer.invoke 异步调用主进程的 window:* 通道，
   * 不经过 `on` 方法的白名单（白名单只作用于 ipcRenderer.on 监听通道）。
   */
  window: {
    /** 创建并返回子窗口；id 重复时聚焦已有窗口 */
    open(
      opts: ChildWindowOpenOptions,
    ): Promise<{ id: string; success: boolean }>;
    /** 关闭指定窗口并从注册表移除 */
    close(id: string): Promise<void>;
    /** 聚焦指定窗口 */
    focus(id: string): Promise<void>;
    /** 移动指定窗口 */
    move(id: string, pos: { x: number; y: number }): Promise<void>;
    /** 调整指定窗口大小 */
    resize(id: string, size: { width: number; height: number }): Promise<void>;
  };

  /** 是否为子窗口/悬浮窗（main.ts 经 additionalArguments 注入 --agentos-child-window）；
   *  前端 TitleBar 仅在主窗口（false）渲染 */
  isChildWindow: boolean;

  /** 主窗口自控 API（自定义标题栏按钮；按发起调用的窗口自身生效） */
  /** 认证会话镜像（强杀耐久备份）：refresh token 主进程落盘，跨重启自动登录 */
  authSession: {
    save(refreshToken: string | null): Promise<boolean>;
    load(): Promise<string | null>;
  };
  /** 装机版自动登录凭据（ADR 2026-10-02-packaged-auto-login-env-only）：load 仅装机形态且 launcher 已播种 AGENTOS_ADMIN_PASSWORD 时返回非空 */
  adminCredential: {
    load(): Promise<{ username: string; password: string } | null>;
  };
  /** 远端内核实时通道中继（app:// 安全源发 ws:// 远端属混合内容被浏览器拦） */
  wsRelay: {
    /** 声明当前内核基址（中继只转发到它，非开放代理） */
    allow(target: string | null): Promise<boolean>;
    /** 取中继 URL（票据/协议版本/补漏游标随行） */
    url(params: WsRelayUrlParams): Promise<string | null>;
  };
  windowControls: {
    /** 最小化 */
    minimize(): Promise<void>;
    /** 最大化/还原切换；返回切换后的最大化状态 */
    toggleMaximize(): Promise<boolean>;
    /** 关闭（主窗口 = 收进托盘，与原生 X 行为一致） */
    close(): Promise<void>;
    /** 查询当前最大化状态 */
    isMaximized(): Promise<boolean>;
    /**
     * 监听最大化状态变化（系统路径触发：双击拖拽区、Win+方向键等）。
     *
     * @param callback - 接收最大化状态的回调函数
     * @returns 取消监听的函数
     */
    onMaximizedChange(callback: (maximized: boolean) => void): () => void;
  };

  /** 系统通知子 API（宿主 OS 原生通知：Windows toast / macOS 通知中心 / Linux libnotify） */
  notification: {
    /** 弹出系统通知；返回是否成功弹出（宿主不支持/参数非法为 false）。
     *  threadId 非空时点击通知额外派发 notification:navigate（跳转对应对话） */
    show(opts: { title: string; body: string; threadId?: string }): Promise<boolean>;
    /** 订阅系统通知点击导航；返回取消订阅函数 */
    onNotificationNavigate(
      callback: (payload: { threadId?: string }) => void,
    ): () => void;
  };

  /** 原生对话框子 API（系统资源管理器） */
  dialog: {
    /** 选择目录；返回绝对路径，用户取消为 null */
    pickDirectory(): Promise<string | null>;
  };

  /** 应用壳进程树内存与 CPU 指标子 API（监控页全口径内存的应用壳段） */
  appMetrics: {
    /** 汇总 Electron 全部进程（主/渲染/GPU/utility）物理内存（KB 口径）与 CPU（percentCPUUsage 求和） */
    get(): Promise<{
      processCount: number;
      totalWorkingSetKb: number;
      totalCpuPercent: number;
    }>;
  };

  /** 更新提示子 API（P-24，不做自动更新；主进程启动时检查一次版本清单） */
  update: {
    /** 拉取当前已知的新版本信息（未检查/同版本/检查失败 = null） */
    getAvailable(): Promise<{
      latestVersion: string;
      downloadUrl: string;
    } | null>;
    /** 订阅新版本可用推送（主进程 update:available）；返回取消订阅函数 */
    onAvailable(
      callback: (info: { latestVersion: string; downloadUrl: string }) => void,
    ): () => void;
  };
}

/** window:open 的参数（与 main.ts 的 ChildWindowOptions 对齐） */
export interface ChildWindowOpenOptions {
  /** 窗口标识（前端传入，用于后续 close/focus） */
  id: string;
  /** 加载的 URL（如 'http://localhost:5188/#/p/my-page' 深链） */
  url: string;
  title?: string;
  width?: number;
  height?: number;
  x?: number;
  y?: number;
  frame?: boolean;
  transparent?: boolean;
  alwaysOnTop?: boolean;
  skipTaskbar?: boolean;
}

/** 子窗口标记字面量（与 main.ts 的 CHILD_WINDOW_ARG 保持一致） */
const CHILD_WINDOW_ARG = "--agentos-child-window";

/** 最大化状态推送通道（与 main.ts 的 MAXIMIZED_CHANGED_CHANNEL 保持一致） */
const MAXIMIZED_CHANGED_CHANNEL = "window:maximized-changed";

// 通过 contextBridge 安全暴露 API
contextBridge.exposeInMainWorld("electronAPI", {
  /** 认证会话镜像（强杀耐久备份）：tokenLifecycle 写入/清除时镜像，启动时回读 */
  authSession: {
    save: (refreshToken: string | null) => {
      return ipcRenderer.invoke(
        "auth:session:save",
        refreshToken,
      ) as Promise<boolean>;
    },
    load: () => {
      return ipcRenderer.invoke("auth:session:load") as Promise<string | null>;
    },
  },
  /** 装机版自动登录凭据（ADR 2026-10-02-packaged-auto-login-env-only）：load 仅装机形态且 launcher 已播种 AGENTOS_ADMIN_PASSWORD 时返回非空 */
  /** 远端内核实时通道中继（app:// 安全源发 ws:// 远端属混合内容被浏览器拦） */
  wsRelay: {
    /** 声明当前内核基址（中继只转发到它，非开放代理） */
    allow: (target: string | null) => {
      return ipcRenderer.invoke("ws:relay:allow", target) as Promise<boolean>;
    },
    /** 取中继 URL（票据/协议版本/补漏游标随行） */
    url: (params: WsRelayUrlParams) => {
      return ipcRenderer.invoke("ws:relay:url", params) as Promise<string | null>;
    },
  },
  adminCredential: {
    load: () => {
      return ipcRenderer.invoke("auth:admin-credential:load") as Promise<{
        username: string;
        password: string;
      } | null>;
    },
  },


  /**
   * 获取应用版本号。
   */
  getAppVersion: (): string => {
    return ipcRenderer.sendSync("get-app-version") as string;
  },

  /**
   * 获取当前运行平台。
   */
  getPlatform: (): string => {
    return ipcRenderer.sendSync("get-platform") as string;
  },

  /**
   * 窗口管理子 API（P2/P3 多窗口基础设施）。
   *
   * 所有方法封装 ipcRenderer.invoke('window:*')，主进程由 main.ts 的
   * ipcMain.handle 注册。invoke 绕过 on 的白名单（白名单只约束 on 监听），
   * 安全性由主进程的参数校验保证。
   */
  window: {
    open: (opts: ChildWindowOpenOptions) => {
      return ipcRenderer.invoke("window:open", opts) as Promise<{
        id: string;
        success: boolean;
      }>;
    },
    close: (id: string) => {
      return ipcRenderer.invoke("window:close", id) as Promise<void>;
    },
    focus: (id: string) => {
      return ipcRenderer.invoke("window:focus", id) as Promise<void>;
    },
    move: (id: string, pos: { x: number; y: number }) => {
      return ipcRenderer.invoke("window:move", id, pos) as Promise<void>;
    },
    resize: (id: string, size: { width: number; height: number }) => {
      return ipcRenderer.invoke("window:resize", id, size) as Promise<void>;
    },
  },

  /** 子窗口标记：main.ts 创建子窗口时经 additionalArguments 注入命令行参数 */
  isChildWindow: process.argv.includes(CHILD_WINDOW_ARG),

  /**
   * 主窗口自控 API（自定义标题栏按钮）。
   * invoke 通道为 window:self:*，主进程按 event.sender 反查窗口，
   * 只作用于发起调用的窗口自身。
   */
  windowControls: {
    minimize: () => {
      return ipcRenderer.invoke("window:self:minimize") as Promise<void>;
    },
    toggleMaximize: () => {
      return ipcRenderer.invoke(
        "window:self:maximize-toggle",
      ) as Promise<boolean>;
    },
    close: () => {
      return ipcRenderer.invoke("window:self:close") as Promise<void>;
    },
    isMaximized: () => {
      return ipcRenderer.invoke("window:self:is-maximized") as Promise<boolean>;
    },
    onMaximizedChange: (
      callback: (maximized: boolean) => void,
    ): (() => void) => {
      const handler = (
        _event: Electron.IpcRendererEvent,
        maximized: boolean,
      ): void => {
        callback(maximized);
      };
      ipcRenderer.on(MAXIMIZED_CHANGED_CHANNEL, handler);

      return () => {
        ipcRenderer.removeListener(MAXIMIZED_CHANGED_CHANNEL, handler);
      };
    },
  },

  /**
   * 系统通知子 API。invoke 通道为 notification:show，主进程用 Electron
   * Notification 按宿主 OS 路由（Windows toast / macOS 通知中心 / Linux
   * libnotify）；提示音仍由渲染进程 Web Audio 合成，主进程侧静音。
   * show 携带 threadId 时，点击通知主进程额外派发 notification:navigate
   * （经 onNotificationNavigate 订阅消费，跳转到通知对应的会话）。
   */
  notification: {
    show: (opts: { title: string; body: string; threadId?: string }) => {
      return ipcRenderer.invoke("notification:show", opts) as Promise<boolean>;
    },
    /** 订阅系统通知点击导航；返回取消订阅函数 */
    onNotificationNavigate: (
      callback: (payload: { threadId?: string }) => void,
    ): (() => void) => {
      const handler = (
        _event: Electron.IpcRendererEvent,
        payload: { threadId?: string },
      ): void => {
        callback(payload);
      };
      ipcRenderer.on("notification:navigate", handler);

      return () => {
        ipcRenderer.removeListener("notification:navigate", handler);
      };
    },
  },

  /**
   * 原生对话框子 API。invoke 通道为 dialog:pick-directory，主进程用
   * Electron dialog.showOpenDialog 以发起窗口为父窗模态弹出资源管理器
   * 目录选择；选中返回绝对路径，用户取消为 null。
   */
  dialog: {
    pickDirectory: () => {
      return ipcRenderer.invoke("dialog:pick-directory") as Promise<
        string | null
      >;
    },
  },

  /**
   * 应用壳进程树内存与 CPU 指标。invoke 通道为 app:metrics，主进程汇总
   * getAppMetrics 全部进程的物理内存（workingSetSize，KB 口径）与 CPU
   * （cpu.percentCPUUsage 求和）。
   */
  appMetrics: {
    get: () => {
      return ipcRenderer.invoke("app:metrics") as Promise<{
        processCount: number;
        totalWorkingSetKb: number;
        totalCpuPercent: number;
      }>;
    },
  },

  /**
   * 更新提示子 API（P-24，不做自动更新）。invoke 通道为 update:get-available
   * （拉取启动检查结果的兜底——渲染层挂载晚于主进程推送时补拿）；推送通道
   * update:available 由主进程检查到新版本时经 webContents.send 发出。
   * 仅提示版本与下载地址，不下载不安装。
   */
  update: {
    getAvailable: () => {
      return ipcRenderer.invoke("update:get-available") as Promise<{
        latestVersion: string;
        downloadUrl: string;
      } | null>;
    },
    onAvailable: (
      callback: (info: { latestVersion: string; downloadUrl: string }) => void,
    ): (() => void) => {
      const handler = (
        _event: Electron.IpcRendererEvent,
        info: { latestVersion: string; downloadUrl: string },
      ): void => {
        callback(info);
      };
      ipcRenderer.on("update:available", handler);

      return () => {
        ipcRenderer.removeListener("update:available", handler);
      };
    },
  },
} satisfies ElectronAPI);
