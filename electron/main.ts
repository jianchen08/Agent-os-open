/**
 * Electron 主进程入口。
 *
 * 创建 BrowserWindow 加载 React 前端（开发时加载 Vite dev server，
 * 生产时经 app:// 自定义协议加载 dist/index.html，见 app-protocol.ts），
 * 集成系统托盘、全局快捷键和窗口信息采集。
 */

import {
  app,
  BrowserWindow,
  dialog,
  globalShortcut,
  ipcMain,
  Menu,
  Notification,
  screen,
  shell,
} from "electron";
import { createHash } from "crypto";
import * as fs from "fs";
import * as path from "path";

import {
  APP_BASE_URL,
  APP_LOADING_URL,
  installAppProtocolHandler,
  isAppSource,
  registerAppSchemePrivileges,
  shouldBlockTopFrameNavigation,
} from "./app-protocol";
import {
  applyOfflineVenvEnv,
  ensurePackagedKernelRunning,
  ensureUvForPackagedBoot,
  getPackagedAdminCredential,
  kernelResourcePaths,
  managedKernelLogDir,
  patrolPluginVenvs,
  shutdownManagedKernel,
  TOKEN_SECRET_FILE,
} from "./kernel-manager";
import { initDiagLog, logDiag } from "./diag-log";
import {
  KERNEL_LIVENESS_STALE_THRESHOLD_MS,
  KERNEL_LIVENESS_UNKNOWN,
  LIVENESS_MARKER_FILENAME,
  startKernelLivenessPoller,
  type KernelLivenessPoller,
  type KernelLivenessSnapshot,
} from "./kernel-liveness";
import {
  reportRenderProcessGone,
  reportWebContentsResponsive,
  reportWebContentsUnresponsive,
} from "./renderer-health";
import { startWsRelay, type WsRelayHandle } from "./ws-relay";
import { attachEditMenu } from "./edit-menu";
import { createTray, destroyTray } from "./tray";
import {
  UPDATE_GET_AVAILABLE_CHANNEL,
  currentUpdateInfo,
  startUpdateCheck,
} from "./update-check";
import {
  AUTH_SESSION_FILE,
  loadAuthSession,
  saveAuthSession,
} from "./auth-session";
import {
  captureWindowState,
  loadWindowState,
  restoredWindowOptions,
  saveWindowState,
} from "./window-state";
import {
  isVisibilityWatchdogEnabled,
  VisibilityRecovery,
} from "./visibility-recovery";

// 协议特权注册必须在 app ready 之前（模块加载即执行）
registerAppSchemePrivileges();

// 进程级兜底日志：任何逃逸到顶层的异常/拒绝都必须留下可诊断输出，
// 禁止无日志死路（BUG-50 的反馈缺口：启动序列中断后无任何痕迹）
process.on("uncaughtException", (err) => {
  console.error("[Electron] 未捕获异常:", err);
  logDiag(`[main] uncaughtException: ${err?.stack ?? err}`);
});
process.on("unhandledRejection", (reason) => {
  console.error("[Electron] 未处理的 Promise 拒绝:", reason);
  logDiag(`[main] unhandledRejection: ${String(reason)}`);
});

/** 开发环境下 Vite dev server 的 URL（端口 5188，与 vite.config.ts 一致） */
const VITE_DEV_SERVER_URL = "http://localhost:5188";

/** 全局快捷键：Ctrl+Shift+A 切换窗口显示/隐藏 */
const TOGGLE_SHORTCUT = "Ctrl+Shift+A";

/**
 * Windows 系统通知（toast）的 AppUserModelID，与 package.json build.appId 一致：
 * electron-builder NSIS 安装的开始菜单快捷方式即以此 AUMID 创建，进程侧
 * 必须声明同一 ID，toast 才能归属到应用（否则不显示或归到 electron 名下）。
 */
const APP_USER_MODEL_ID = "com.agentos.assistant";

/**
 * 子窗口标记（preload.ts 的 isChildWindow 依据同一字面量）。
 * 子窗口/悬浮窗经 webPreferences.additionalArguments 携带，前端 TitleBar
 * 据此只在主窗口渲染（子浮窗保持无边框悬浮组件形态，不出现标题栏）。
 */
const CHILD_WINDOW_ARG = "--agentos-child-window";

/** 最大化状态推送通道（preload.ts 的 windowControls.onMaximizedChange 监听同一通道） */
const MAXIMIZED_CHANGED_CHANNEL = "window:maximized-changed";

/** 主窗口引用 */
let mainWindow: BrowserWindow | null = null;

/** 渲染层可见性自愈控制器（主窗口一个实例，见 visibility-recovery.ts） */
let visibilityRecovery: VisibilityRecovery | null = null;

/**
 * 子窗口/悬浮窗注册表
 *
 * key = 前端传入的窗口 id；value = BrowserWindow。
 * 创建时入表，窗口 'closed' 事件触发时自动出表。
 */
const childWindows = new Map<string, BrowserWindow>();

/**
 * 创建子窗口/悬浮窗的参数（与前端 ElectronOpenWindowOptions 对齐）。
 * 由前端经 ipcRenderer.invoke('window:open', opts) 传入。
 */
export interface ChildWindowOptions {
  /** 窗口标识（前端传入，用于后续 close/focus） */
  id: string;
  /** 加载的 URL（如 'http://localhost:5188/#/p/my-page' 深链） */
  url: string;
  /** 窗口标题 */
  title?: string;
  /** 窗口宽度，默认 320 */
  width?: number;
  /** 窗口高度，默认 480 */
  height?: number;
  /** 窗口左上角 X（不传则居中） */
  x?: number;
  /** 窗口左上角 Y（不传则居中） */
  y?: number;
  /** 是否有边框，默认 false（悬浮组件样式） */
  frame?: boolean;
  /** 是否透明，默认 false */
  transparent?: boolean;
  /** 是否置顶，默认 false */
  alwaysOnTop?: boolean;
  /** 是否隐藏任务栏图标，默认 false */
  skipTaskbar?: boolean;
}

/** 子窗口默认宽度（与前端 toElectronOpenOptions 对齐） */
const DEFAULT_CHILD_WIDTH = 320;
/** 子窗口默认高度 */
const DEFAULT_CHILD_HEIGHT = 480;

/**
 * 把前端传入的 opts 合并默认值，生成最终的窗口参数（纯函数，便于单测）。
 *
 * - width/height 缺省时回落到 DEFAULT_CHILD_WIDTH/HEIGHT
 * - frame/transparent/alwaysOnTop/skipTaskbar 缺省时为 false
 * - x/y 允许缺省（由调用方决定居中策略）
 *
 * 不接触 BrowserWindow，故可在无 Electron 运行时环境下测试。
 */
export function resolveChildWindowOptions(
  opts: ChildWindowOptions,
): Required<
  Pick<
    ChildWindowOptions,
    | "id"
    | "url"
    | "width"
    | "height"
    | "frame"
    | "transparent"
    | "alwaysOnTop"
    | "skipTaskbar"
  >
> &
  Pick<ChildWindowOptions, "title" | "x" | "y"> {
  return {
    id: opts.id,
    url: opts.url,
    title: opts.title,
    width: opts.width ?? DEFAULT_CHILD_WIDTH,
    height: opts.height ?? DEFAULT_CHILD_HEIGHT,
    x: opts.x,
    y: opts.y,
    frame: opts.frame ?? false,
    transparent: opts.transparent ?? false,
    alwaysOnTop: opts.alwaysOnTop ?? false,
    skipTaskbar: opts.skipTaskbar ?? false,
  };
}

/**
 * 判断当前是否为开发环境。
 *
 * 通过 ELECTRON_IS_DEV 环境变量或 app.isPackaged 属性判断。
 */
function isDevelopment(): boolean {
  return process.env.ELECTRON_IS_DEV === "1" || !app.isPackaged;
}

/**
 * 窗口导航硬化（安全审查 B-5 + 死页类级清理）：
 *  - setWindowOpenHandler：target=_blank 类新窗请求一律拦截，http(s) 外链交给
 *    系统浏览器（协议白名单），其余协议（file:/javascript: 等）静默拒绝；
 *  - will-navigate：非 app/dev 源阻止（防导航劫持）；app/dev 源命中内核代理面
 *    （/api /ext /media /uploads）同样阻止——裸内核响应（404 "not found" 等）
 *    不得整窗替换应用 UI。政策单一来源见 app-protocol.shouldBlockTopFrameNavigation。
 */
function hardenWindowNavigation(win: BrowserWindow): void {
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, url) => {
    if (shouldBlockTopFrameNavigation(url)) {
      event.preventDefault();
    }
  });
}

/**
 * 窗口状态存档（关闭/退出前调用）。失败只留告警不阻断：状态记忆是尽力而为
 * 的旁路，绝不能反过来卡住关窗/退出主链。
 */
function persistMainWindowState(win: BrowserWindow): void {
  try {
    saveWindowState(app.getPath("userData"), captureWindowState(win));
  } catch (err) {
    console.warn("[Electron] 窗口状态存档失败:", err);
  }
}

/**
 * 创建主窗口。
 *
 * 开发环境加载 Vite dev server URL，生产环境加载构建后的 index.html。
 * 创建后注册快捷键、初始化托盘和窗口信息轮询。
 *
 * @param loadContent - 首屏内容加载器（默认按 dev/prod 分流，见 loadInitialContent）
 */
function createMainWindow(
  loadContent: (win: BrowserWindow) => void = loadInitialContent,
): BrowserWindow {
  // 窗口状态恢复：跨启动记忆位置/尺寸/最大化（存档点见 persistMainWindowState）；
  // 无有效存档（首启/档案损坏/显示器校验不过）回落默认 1200×800 居中
  const restored = restoredWindowOptions(
    loadWindowState(app.getPath("userData")),
    screen.getAllDisplays().map((d) => d.workArea),
    { width: 1200, height: 800 },
  );
  const win = new BrowserWindow({
    width: restored.width,
    height: restored.height,
    ...(restored.x !== undefined && restored.y !== undefined
      ? { x: restored.x, y: restored.y }
      : {}),
    minWidth: 800,
    minHeight: 600,
    // 自定义标题栏：Windows/macOS 隐藏原生标题栏（保留系统边框/缩放/Snap），
    // Linux 不支持 titleBarStyle 用整体无边框；窗口控制（最小化/最大化/关闭）
    // 由前端 TitleBar 组件经 window:self:* IPC 承担
    ...(process.platform === "linux"
      ? { frame: false }
      : { titleBarStyle: "hidden" as const }),
    // 开发环境菜单栏默认隐藏（Alt 唤出，保留 DevTools/刷新快捷键）；
    // 生产环境在 whenReady 中整个移除应用菜单
    autoHideMenuBar: true,
    show: false, // 先隐藏，ready-to-show 后再显示
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
    // 窗口图标
    icon: resolveAppIcon(),
  });

  hardenWindowNavigation(win);
  // 编辑面：右键剪切/复制/粘贴/全选菜单。键盘 Ctrl+C/X/V/A 走 Chromium 原生
  // 路径（独立于应用菜单生效），不经菜单也不经主进程代理——见 edit-menu.ts 头注释
  attachEditMenu(win);

  // 渲染进程挂死/恢复观测（证据面先行，不自动重启不弹窗；处理函数见
  // renderer-health.ts）。随窗口创建重挂（macOS activate 重建窗口路径）。
  win.webContents.on("unresponsive", () => {
    reportWebContentsUnresponsive(webContentsProcessId(win.webContents), logDiag);
  });
  win.webContents.on("responsive", () => {
    reportWebContentsResponsive(webContentsProcessId(win.webContents), logDiag);
  });

  // 存档为最大化时先最大化再显示（窗口此刻隐藏，无闪动）；TitleBar 挂载时
  // 经 window:self:is-maximized 同步图标，maximize 事件推送照常覆盖
  if (restored.isMaximized) {
    win.maximize();
  }

  // 最大化状态变化推送前端（TitleBar 的最大化/还原图标切换；
  // 双击拖拽区、Win+方向键等系统路径触发的变化同样覆盖）
  const sendMaximized = (maximized: boolean): void => {
    if (!win.isDestroyed()) {
      win.webContents.send(MAXIMIZED_CHANGED_CHANNEL, maximized);
    }
  };
  win.on("maximize", () => sendMaximized(true));
  win.on("unmaximize", () => sendMaximized(false));

  // 渲染层可见性自愈：显示/还原后延迟校验 visibilityState（控制器随窗口
  // 创建并挂兜底巡检；"show" 在 ready-to-show 之后才首次触发，届时引用已
  // 就绪。微抖自身的 hide→show 回声由控制器 inFlight 挡住）
  const notifyVisibilityRecovery = (): void => {
    visibilityRecovery?.onWindowShown();
  };
  win.on("show", notifyVisibilityRecovery);
  win.on("restore", notifyVisibilityRecovery);
  // "focus" 钩子补"用户切回本应用"的复查时机：失联判定以窗口持有 OS 焦点
  // 为前提（窗口被别的窗口盖住时 Chromium 报 hidden 属正常遮挡，见
  // visibility-recovery.ts 头注释），用户切回来即需复查一次。focus 入口
  // 不重置微抖预算——否则用户每点一次应用就再武装一轮 3 次 hide→show。
  win.on("focus", () => {
    visibilityRecovery?.onWindowFocused();
  });
  // 控制器与窗口同生命周期（macOS activate 重建窗口时旧实例随替换）
  visibilityRecovery?.stop();
  visibilityRecovery = null;
  if (isVisibilityWatchdogEnabled(process.env)) {
    // 诊断落盘：决策点事件进 userData/logs/visibility-recovery.log——主进程
    // stdout 脱管启动即丢，R371 现场失效（17 分钟未自愈）无据可查的教训；
    // 写失败不阻断自愈，但必须留痕（诊断面失明即回到无据可查）。
    const visibilitySink = (line: string): void => {
      try {
        const dir = path.join(app.getPath("userData"), "logs");
        fs.mkdirSync(dir, { recursive: true });
        fs.appendFileSync(path.join(dir, "visibility-recovery.log"), line + "\n");
      } catch (err) {
        console.warn("[Electron] visibility 诊断落盘失败:", err);
      }
    };
    visibilityRecovery = new VisibilityRecovery(win, { sink: visibilitySink });
    visibilityRecovery.startPolling();
  }

  // 窗口准备好后显示
  win.once("ready-to-show", () => {
    win.show();
  });

  // 加载前端页面
  loadContent(win);

  // 窗口关闭时隐藏而非退出（配合托盘使用）；关闭即存档（hide-to-tray 是
  // 最频繁的状态存档点，后续托盘重开/再次启动都能拿到最新边界）
  win.on("close", (event) => {
    persistMainWindowState(win);
    event.preventDefault();
    win.hide();
  });

  mainWindow = win;
  return win;
}

/**
 * 加载前端页面。
 *
 * 开发环境加载 Vite dev server URL，生产环境经 app:// 协议加载 dist/index.html
 * （app-protocol.ts 负责静态伺服、SPA 深链回落与内核 API 代理）。
 *
 * 子窗口场景（opts.url 提供）：url 已是完整应用源深链（dev 为 hash 形态
 * http://localhost:5188/#/p/<pageId>，prod 为 pathname 形态 app://bundle/p/<pageId>，
 * 形态分派见前端 buildChildWindowUrl），直接 loadURL。
 *
 * @param win - BrowserWindow 实例
 * @param opts - 可选，url 为子窗口深链 URL
 */
function loadFrontend(win: BrowserWindow, opts?: { url?: string }): void {
  if (opts?.url) {
    win.loadURL(opts.url).catch((err) => {
      console.error("[Electron] 加载子窗口页面失败:", err);
    });
    console.info(`[Electron] 子窗口加载: ${opts.url}`);
    return;
  }

  if (isDevelopment()) {
    win.loadURL(VITE_DEV_SERVER_URL).catch((err) => {
      console.error(
        `[Electron] 加载 Vite dev server 失败（dev server 未起？）: ${VITE_DEV_SERVER_URL}`,
        err,
      );
    });
    // 开发环境打开 DevTools
    win.webContents.openDevTools({ mode: "detach" });
    console.info(
      `[Electron] 开发模式，加载 Vite dev server: ${VITE_DEV_SERVER_URL}`,
    );
  } else {
    win.loadURL(`${APP_BASE_URL}/index.html`).catch((err) => {
      console.error("[Electron] 加载前端页面失败:", err);
    });
    console.info(`[Electron] 生产模式，加载: ${APP_BASE_URL}/index.html`);
  }
}

/**
 * 生产首屏加载态页（app://bundle/__loading.html，由 app-protocol 协议内路由
 * 伺服）。加载失败只降级 UI（立即显示窗口，黑窗等待真实前端），绝不影响
 * 内核拉起序列——两条链路各自独立，此处任何异常都不得中断 bootPackagedKernel。
 */
function showKernelLoadingPage(win: BrowserWindow): void {
  win.loadURL(APP_LOADING_URL).catch((err) => {
    // ERR_ABORTED = 加载态被后续真实内容导航取代（内核秒就绪时），属预期
    if ((err as { code?: string })?.code !== "ERR_ABORTED") {
      console.error(
        "[Electron] 加载内核启动加载态失败（不阻断内核拉起）:",
        err,
      );
      if (!win.isDestroyed()) {
        win.show();
      }
    }
  });
}

/** 首屏加载分流：dev 直载前端；生产先显示加载态，内核就绪后再载前端。
 *  加载态与内核拉起互不依赖：前者失败只降级 UI，后者自带失败兜底
 *  （错误对话框 + 退出），任一失败都不阻断另一条。 */
function loadInitialContent(win: BrowserWindow): void {
  if (isDevelopment()) {
    loadFrontend(win);
    return;
  }
  showKernelLoadingPage(win);
  void bootPackagedKernelThenLoad(win);
}

/**
 * 拉起打包件内核（只 spawn 包内内核，见 kernel-manager）并在健康就绪后载入前端。
 * 失败（安装损坏/启动失败/就绪超时）一律显式错误对话框 + 退出，不静默白屏。
 */
export async function bootPackagedKernelThenLoad(
  win: BrowserWindow,
): Promise<void> {
  try {
    // uv 引导（2026-10-26 用户裁定「安装上就能用」）：本机 PATH 有 uv 用本机的
    // （不另装）；没有就把随包 prereqs/uv.exe 落 <用户根>/bin 并前置进程 PATH
    // ——内核 autoprovision 与全部子 spawn 继承。缺失不可补 → 日志降级不反噬
    // 启动（与 venv-patrol 同语义，见 kernel-manager ensureUvForPackagedBoot）。
    try {
      const uvDecision = ensureUvForPackagedBoot({
        resourcesPath: process.resourcesPath,
        appDataDir: app.getPath("appData"),
        userRootEnv: process.env.AGENTOS_USER_ROOT,
        env: process.env,
      });
      if (uvDecision.action === "materialize") {
        logDiag(`[uv-bootstrap] 随包 uv 已落位并前置 PATH: ${uvDecision.targetUvPath}`);
      } else if (uvDecision.action === "unavailable") {
        logDiag(`[uv-bootstrap] uv 不可用（${uvDecision.reason}）`);
      } else {
        logDiag("[uv-bootstrap] 本机 uv 在 PATH，未做改动");
      }
      // 离线 venv 供给：随包 python/wheels 在场才注入（旧包/dev 零改动）。
      const offline = applyOfflineVenvEnv({
        resourcesPath: process.resourcesPath,
        env: process.env,
      });
      if (offline.pythonShipped || offline.wheelhouseShipped) {
        logDiag(
          `[uv-bootstrap] 离线供给注入 python=${offline.pythonShipped} wheelhouse=${offline.wheelhouseShipped}`,
        );
      }
    } catch (uvErr) {
      logDiag(`[uv-bootstrap] 启动跳过: ${String(uvErr)}`);
    }
    const result = await ensurePackagedKernelRunning({
      resourcesPath: process.resourcesPath,
      // token 签名密钥持久化（认证身份目录，机器级跨安装稳定）：刷新令牌跨
      // 重启有效，否则内核每进程随机签名，用户每次打开应用都要重新登录；
      // 不随安装目录哈希走——换目录重装不再作废全端令牌（2026-10-22 用户实测）
      authIdentityDir,
      // 库位置钉用户根（BUG-85）：装机默认库=<appData>/agentos/agentos_kernel.db，
      // 安装目录不再落库，静默卸载不再删用户数据
      appDataDir: app.getPath("appData"),
    });
    console.info(
      `[Electron] 内核就绪（已拉起打包内核 pid=${result.pid ?? "?"}），加载前端`,
    );
    // 内核日志面判活轮询（60s 周期读 resources/kernel/logs/kernel.liveness，
    // 停摆/恢复跃迁落 diag-log；判活快照并入 app:metrics 见 kernel-liveness.ts）。
    // 观测面失败不反噬启动主流程。
    try {
      startKernelLivenessWatch();
    } catch (livenessErr) {
      logDiag(`[kernel-liveness] 启动跳过: ${String(livenessErr)}`);
    }
    // 插件 venv 健康巡检（boot 后台，不阻塞前端加载）：断言「解释器在 + SDK 可
    // import」，不健康即补装并再断言。venv 重建成功≠可用（装机 2026-10-18 三犯：
    // 残缺 venv 无断言静默传播→合宿组全灭 50 分钟），此处是 launcher 侧断言面；
    // uv 缺席/失败仅日志降级（与内核 autoprovision 同语义）。观测面失败不反噬
    // 启动主流程——独立兜底，失败只留日志。
    try {
      const pluginsDir = kernelResourcePaths(process.resourcesPath).pluginsDir;
      void patrolPluginVenvs({ pluginsDir }).then((report) => {
        const broken = report.broken.map((b) => b.key);
        logDiag(
          `[venv-patrol] ok=${report.ok.length} repaired=${report.repaired.length} ` +
            `broken=${JSON.stringify(broken)}`,
        );
        if (broken.length > 0) {
          logDiag(`[venv-patrol] 仍故障: ${broken.join(",")}（详见上方 [VenvPatrol] 行）`);
        }
      });
    } catch (patrolErr) {
      logDiag(`[venv-patrol] 启动跳过（插件根解析失败）: ${String(patrolErr)}`);
    }
  } catch (err) {
    // 错误消息自带用户指引（缺失→重装 / 超时→看日志重试），此处原样呈现
    const message = err instanceof Error ? err.message : String(err);
    console.error("[Electron] 内核启动失败:", message);
    logDiag(`[main] 内核启动失败 -> app.exit(1): ${message}`);
    dialog.showErrorBox("灵汐助手 启动失败", message);
    app.exit(1);
    return;
  }
  if (!win.isDestroyed()) {
    loadFrontend(win);
  }
}

/**
 * 解析应用图标路径。
 *
 * @returns 图标文件路径，找不到时返回 undefined
 */
function resolveAppIcon(): string | undefined {
  const iconCandidates = [
    path.join(app.getAppPath(), "frontend", "public", "favicon.ico"),
    path.join(app.getAppPath(), "frontend", "public", "icon.png"),
    path.join(app.getAppPath(), "build", "icon.png"),
    path.join(app.getAppPath(), "build", "favicon.ico"),
  ];

  for (const iconPath of iconCandidates) {
    try {
      const fs = require("fs") as typeof import("fs");
      if (fs.existsSync(iconPath)) {
        return iconPath;
      }
    } catch {
      // 忽略
    }
  }
  return undefined;
}

/**
 * 注册全局快捷键 Ctrl+Shift+A 切换窗口显示/隐藏。
 */
function registerGlobalShortcut(): void {
  const success = globalShortcut.register(TOGGLE_SHORTCUT, () => {
    toggleMainWindow();
  });

  if (!success) {
    console.warn(`[Electron] 快捷键 ${TOGGLE_SHORTCUT} 注册失败，可能已被占用`);
  } else {
    console.info(`[Electron] 快捷键 ${TOGGLE_SHORTCUT} 注册成功`);
  }
}

/**
 * 切换主窗口的显示/隐藏状态。
 */
function toggleMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  if (mainWindow.isVisible()) {
    mainWindow.hide();
  } else {
    mainWindow.show();
    mainWindow.focus();
  }
}

/**
 * 把主窗口带到前台（还原最小化 → 显示 → 聚焦）。
 *
 * 窗口不存在/已销毁时静默返回。second-instance（双开唤起首实例）与
 * 系统通知点击共用同一恢复序列。
 *
 * show 无条件调用：Windows 前台锁下，窗口可见但在后台时单独 focus() 被
 * OS 静默拒绝（toast 点击激活语境进程无前台权，窗口纹丝不动=「点击通知
 * 无反应」），show 自带激活语义才真正置前。
 */
export function focusMainWindow(win: BrowserWindow | null): void {
  if (!win || win.isDestroyed()) {
    return;
  }
  if (win.isMinimized()) {
    win.restore();
  }
  win.show();
  win.focus();
}

/**
 * 系统通知点击统一处理：聚焦主窗口；携带 threadId 时向渲染进程派发
 * notification:navigate（渲染层据此跳转到通知对应的会话）。
 *
 * 窗口未就绪/已销毁时只安全无操作（聚焦路径同 focusMainWindow 的静默语义）。
 */
export function handleNotificationClick(
  win: BrowserWindow | null,
  threadId?: string,
): void {
  focusMainWindow(win);
  if (threadId !== undefined && win && !win.isDestroyed()) {
    win.webContents.send("notification:navigate", { threadId });
  }
}

/**
 * 弹出系统级通知（Windows toast / macOS 通知中心 / Linux libnotify，
 * 由 Electron Notification 按宿主 OS 路由）。
 *
 * - 静音（silent: true）：提示音由渲染进程 Web Audio 统一合成，避免双重响
 * - 点击通知聚焦主窗口；opts.threadId 为非空字符串时额外派发
 *   notification:navigate 载荷（渲染层跳转到该会话）——threadId 缺失/非法
 *   （非字符串/空串）一律退化为仅聚焦，与既有行为兼容
 * - 参数非法或宿主不支持时返回 false，不抛错（调用方按布尔处理）
 *
 * 不接触 ipcMain，便于单测。
 */
export function showSystemNotification(opts: {
  title?: unknown;
  body?: unknown;
  threadId?: unknown;
}): boolean {
  if (!opts || typeof opts.title !== "string" || opts.title.length === 0) {
    console.warn("[Electron] notification:show 参数非法，需要非空 title", opts);
    return false;
  }
  if (!Notification.isSupported()) {
    console.warn("[Electron] 宿主不支持系统通知，跳过");
    return false;
  }
  const body = typeof opts.body === "string" ? opts.body : "";
  const threadId =
    typeof opts.threadId === "string" && opts.threadId.length > 0
      ? opts.threadId
      : undefined;
  const notification = new Notification({
    title: opts.title,
    body,
    silent: true,
  });
  notification.on("click", () => {
    handleNotificationClick(mainWindow, threadId);
  });
  notification.show();
  return true;
}

/**
 * 应用壳进程树内存与 CPU 指标（监控页「插件」tab 全口径内存的应用壳段）。
 *
 * 汇总 app.getAppMetrics() 的全部 Electron 进程（主/渲染/GPU/utility）物理
 * 内存：workingSetSize 为 KB 口径（Electron MemoryInfo），Windows 上与任务
 * 管理器 working set 同义，与插件段 RSS / 内核段 RSS 同口径可加和。
 *
 * CPU 汇总：各进程 cpu.percentCPUUsage（Electron CPUUsage，距上次查询的
 * CPU 百分比，首次查询为 0）逐进程求和为 totalCpuPercent。
 *
 * 不接触 ipcMain，便于单测。
 */
export function collectAppMetrics(): {
  processCount: number;
  totalWorkingSetKb: number;
  totalCpuPercent: number;
} {
  const metrics = app.getAppMetrics();
  return {
    processCount: metrics.length,
    totalWorkingSetKb: metrics.reduce((sum, m) => sum + m.memory.workingSetSize, 0),
    totalCpuPercent: metrics.reduce((sum, m) => sum + m.cpu.percentCPUUsage, 0),
  };
}

/**
 * 弹出系统目录选择对话框（资源管理器），供渲染进程声明工作空间/项目目录。
 *
 * - 以发起调用的窗口为父窗模态弹出；发起窗口缺失（webContents 已销毁等）
 *   退化为无父窗弹出，功能不失效
 * - openDirectory 单一选择；createDirectory 供 macOS 原生「新建文件夹」
 *   （Windows 对话框自带该按钮，声明冗余无害）
 * - 用户取消/未选返回 null，选中返回绝对路径
 *
 * 不接触 ipcMain，便于单测。
 */
export async function pickDirectoryViaDialog(
  event: Electron.IpcMainInvokeEvent,
): Promise<string | null> {
  const parent = BrowserWindow.fromWebContents(event.sender);
  const options: Electron.OpenDialogOptions = {
    properties: ["openDirectory", "createDirectory"],
  };
  const result = parent
    ? await dialog.showOpenDialog(parent, options)
    : await dialog.showOpenDialog(options);
  if (result.canceled) return null;
  return result.filePaths[0] ?? null;
}

/**
 * 创建子窗口/悬浮窗（P2/P3 多窗口基础设施）。
 *
 * - 复用主窗口的 preload + contextIsolation 安全模型
 * - 通过 loadFrontend(win, {url}) 加载前端深链 URL
 * - 不绑定 parent（默认独立）；如需模态父子关系可后续扩展 opts.modal/parent
 * - 注册到 childWindows，'closed' 事件触发时自动出表
 *
 * id 重复时不重建，而是聚焦已有窗口（幂等语义）。
 *
 * @returns 创建/复用的 BrowserWindow 及是否为新建
 */
function createChildWindow(opts: ChildWindowOptions): {
  win: BrowserWindow;
  created: boolean;
} {
  // 幂等：id 已存在则聚焦已有窗口
  const existing = childWindows.get(opts.id);
  if (existing && !existing.isDestroyed()) {
    if (existing.isMinimized()) {
      existing.restore();
    }
    existing.focus();
    return { win: existing, created: false };
  }

  const resolved = resolveChildWindowOptions(opts);

  const win = new BrowserWindow({
    width: resolved.width,
    height: resolved.height,
    x: resolved.x,
    y: resolved.y,
    frame: resolved.frame,
    transparent: resolved.transparent,
    alwaysOnTop: resolved.alwaysOnTop,
    skipTaskbar: resolved.skipTaskbar,
    title: resolved.title,
    show: false, // ready-to-show 后再显示，避免白屏闪烁
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // 子窗口标记：preload 据此暴露 isChildWindow=true，前端不渲染 TitleBar
      additionalArguments: [CHILD_WINDOW_ARG],
    },
  });

  hardenWindowNavigation(win);
  // 主窗口同款编辑面（子窗口输入区同样要能复制粘贴）
  attachEditMenu(win);

  win.once("ready-to-show", () => {
    win.show();
  });

  // 加载子窗口深链 URL（开发模式带 hash 路由）
  loadFrontend(win, { url: opts.url });

  // 关闭时自动从注册表移除（避免悬挂引用）
  win.on("closed", () => {
    childWindows.delete(opts.id);
  });

  childWindows.set(opts.id, win);
  return { win, created: true };
}

/**
 * 注册 IPC 处理程序。
 */
function registerIpcHandlers(): void {
  // 获取应用版本
  ipcMain.on("get-app-version", (event) => {
    event.returnValue = app.getVersion();
  });

  // 获取运行平台
  ipcMain.on("get-platform", (event) => {
    event.returnValue = process.platform;
  });

  // ===== 主窗口自控 IPC（前端 TitleBar 组件的自定义标题栏按钮）=====
  // 按 event.sender 反查发起窗口：只作用于发起调用的窗口自身

  // 最小化
  ipcMain.handle("window:self:minimize", (event) => {
    BrowserWindow.fromWebContents(event.sender)?.minimize();
  });

  // 最大化/还原切换；返回切换后的最大化状态
  ipcMain.handle("window:self:maximize-toggle", (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    if (!win) {
      return false;
    }
    if (win.isMaximized()) {
      win.unmaximize();
    } else {
      win.maximize();
    }
    return win.isMaximized();
  });

  // 关闭：主窗口 close 事件 preventDefault→hide（收进托盘），与原生 X 行为一致
  ipcMain.handle("window:self:close", (event) => {
    BrowserWindow.fromWebContents(event.sender)?.close();
  });

  // 查询当前最大化状态（TitleBar 挂载时同步初始图标）
  ipcMain.handle("window:self:is-maximized", (event) => {
    return BrowserWindow.fromWebContents(event.sender)?.isMaximized() ?? false;
  });

  // WS 中继控制面（远端内核实时通道）：
  //  - allow：渲染层声明当前内核基址（中继只转发到它，非开放代理）
  //  - url：取中继 URL（票据/协议版本/补漏游标随行）
  ipcMain.handle("ws:relay:allow", (_event, target: unknown) => {
    ensureWsRelay()?.setAllowedTarget(typeof target === "string" ? target : null);
    return true;
  });
  ipcMain.handle("ws:relay:url", async (_event, params: unknown) => {
    const relay = ensureWsRelay();
    if (!relay) return null;
    await relay.ready.catch(() => 0);
    return relay.buildUrl((params ?? {}) as Record<string, unknown>);
  });

  // 认证会话镜像（渲染进程 localStorage 的强杀耐久备份）：refresh token 单次
  // 轮换后若 Chromium 批量提交尚未落盘即被强杀，stored 值已被服务端作废——
  // 主进程文件同步写立即落入 OS 页缓存，进程强杀不丢。落认证身份目录（机器
  // 级跨安装稳定）：换目录重装后新 Chromium profile 的 localStorage 为空，
  // 初始化链从镜像回读种子即免重登。
  ipcMain.handle("auth:session:save", (_event, refreshToken: unknown) => {
    if (refreshToken !== null && typeof refreshToken !== "string") {
      return false;
    }
    saveAuthSession(
      authIdentityDir,
      typeof refreshToken === "string" ? refreshToken : null,
    );
    return true;
  });
  ipcMain.handle("auth:session:load", () => {
    return loadAuthSession(authIdentityDir);
  });

  // 装机版自动登录凭据（ADR 2026-10-02-packaged-auto-login-env-only）：load
  // 仅 app.isPackaged 且 launcher 已播种 ambient AGENTOS_ADMIN_PASSWORD 时
  // 非空——dev/浏览器形态恒 null，渲染进程据此跳过自动登录；env 即口令
  // 事实源，无回写通道（应用内改密由内核「env 不符即重置」语义对齐）。
  ipcMain.handle("auth:admin-credential:load", () => {
    return app.isPackaged ? getPackagedAdminCredential() : null;
  });

  // ===== P2/P3 多窗口 IPC（ipcMain.handle,支持 async 返回）=====

  // 创建子窗口/悬浮窗;id 重复则聚焦已有
  ipcMain.handle("window:open", (_event, opts: ChildWindowOptions) => {
    if (!opts || typeof opts.id !== "string" || typeof opts.url !== "string") {
      console.warn("[Electron] window:open 参数非法，需要 {id, url}", opts);
      return { id: opts?.id ?? "", success: false };
    }
    // 子窗口 URL 白名单（安全审查 B-5）：仅应用自身源（dev localhost / prod app:）
    if (!isAppSource(opts.url)) {
      console.warn(`[Electron] window:open 拒绝非应用源 url: ${opts.url}`);
      return { id: opts.id, success: false, reason: "url 不在应用源白名单" };
    }
    try {
      const { created } = createChildWindow(opts);
      console.info(
        `[Electron] window:open ${created ? "created" : "focused"} id="${opts.id}" url="${opts.url}"`,
      );
      return { id: opts.id, success: true };
    } catch (err) {
      console.error(`[Electron] window:open 创建失败 id="${opts.id}":`, err);
      return { id: opts.id, success: false };
    }
  });

  // 关闭指定子窗口并从注册表移除
  ipcMain.handle("window:close", (_event, id: string) => {
    const win = childWindows.get(id);
    if (win && !win.isDestroyed()) {
      // 移除 closed 监听以避免重复出表（destroy 会触发 closed）
      win.removeAllListeners("closed");
      win.close();
    }
    childWindows.delete(id);
    console.info(`[Electron] window:close id="${id}"`);
  });

  // 聚焦指定子窗口
  ipcMain.handle("window:focus", (_event, id: string) => {
    const win = childWindows.get(id);
    if (win && !win.isDestroyed()) {
      if (win.isMinimized()) {
        win.restore();
      }
      win.focus();
    }
  });

  // 移动指定子窗口
  ipcMain.handle(
    "window:move",
    (_event, id: string, pos: { x: number; y: number }) => {
      const win = childWindows.get(id);
      if (
        win &&
        !win.isDestroyed() &&
        pos &&
        typeof pos.x === "number" &&
        typeof pos.y === "number"
      ) {
        win.setPosition(Math.trunc(pos.x), Math.trunc(pos.y));
      }
    },
  );

  // 调整指定子窗口大小
  ipcMain.handle(
    "window:resize",
    (_event, id: string, size: { width: number; height: number }) => {
      const win = childWindows.get(id);
      if (
        win &&
        !win.isDestroyed() &&
        size &&
        typeof size.width === "number" &&
        typeof size.height === "number"
      ) {
        win.setSize(Math.trunc(size.width), Math.trunc(size.height));
      }
    },
  );

  // ===== 系统通知 IPC（渲染进程交互提醒经宿主 OS 原生通知呈现）=====
  ipcMain.handle(
    "notification:show",
    (_event, opts: { title?: unknown; body?: unknown; threadId?: unknown }) => {
      return showSystemNotification(opts);
    },
  );

  // ===== 原生目录选择 IPC（工作空间/项目路径声明经资源管理器选择）=====
  ipcMain.handle("dialog:pick-directory", (event) => {
    return pickDirectoryViaDialog(event);
  });

  // ===== 应用壳进程树内存/CPU + 内核日志判活 IPC（监控页应用壳段）=====
  ipcMain.handle("app:metrics", () => {
    return { ...collectAppMetrics(), kernelLiveness: currentKernelLiveness() };
  });

  // ===== 更新提示 IPC（P-24：启动检查结果的拉取兜底；推送通道 update:available）=====
  ipcMain.handle(UPDATE_GET_AVAILABLE_CHANNEL, () => {
    return currentUpdateInfo();
  });
}

/**
 * 清理所有资源（快捷键、托盘、轮询器、子窗口），准备退出。
 */
function cleanup(): void {
  // 关闭所有子窗口（P2/P3 多窗口基础设施）
  try {
    for (const [id, win] of childWindows) {
      if (!win.isDestroyed()) {
        // 移除 closed 监听避免日志噪音
        win.removeAllListeners("closed");
        win.close();
      }
    }
    childWindows.clear();
    console.info("[Electron] 已关闭所有子窗口");
  } catch (err) {
    console.warn("[Electron] 关闭子窗口失败:", err);
  }

  // 注销所有全局快捷键
  try {
    globalShortcut.unregisterAll();
    console.info("[Electron] 已注销所有全局快捷键");
  } catch (err) {
    console.warn("[Electron] 注销快捷键失败:", err);
  }

  // 停止可见性自愈巡检
  if (visibilityRecovery !== null) {
    visibilityRecovery.stop();
    visibilityRecovery = null;
  }

  // 停止内核日志判活轮询（壳退出清理）
  if (kernelLivenessPoller !== null) {
    kernelLivenessPoller.stop();
    kernelLivenessPoller = null;
  }

  // 销毁托盘
  try {
    destroyTray();
    console.info("[Electron] 已销毁系统托盘");
  } catch (err) {
    console.warn("[Electron] 销毁托盘失败:", err);
  }
}

// ========== 应用生命周期 ==========

/**
 * 打包件 userData 安装身份目录（BUG-50 两遗留之一）。
 *
 * asar 内 package.json 无 productName → app.name 落 name 字段（agent-os），
 * 默认 userData=%APPDATA%\agent-os 与开发环境/其他副本（win-unpacked 等）
 * 共享：磁盘缓存被先到者占用 → 渲染进程网络栈退化（ERR_FAILED），且缓存
 * 损坏跨实例传染。现按安装根（exe 所在目录，小写归一）哈希派生安装身份：
 * 同一安装稳定不变，不同副本互相隔离。
 */
export function resolvePackagedUserData(
  exePath: string,
  appDataPath: string,
  appName: string,
): string {
  const installId = createHash("sha256")
    .update(path.dirname(exePath).toLowerCase())
    .digest("hex")
    .slice(0, 12);
  return path.join(appDataPath, appName, "userdata", installId);
}

/**
 * 认证身份目录（跨安装稳定，机器级）：token 签名密钥与认证会话镜像的落盘位。
 *
 * 有意不随安装身份（resolvePackagedUserData 的安装目录哈希）走：BUG-50 按
 * 安装目录隔离的是 Chromium 磁盘缓存（损坏跨副本传染），认证身份则是机器
 * 级事实源——内核用户库本就机器级共享（%APPDATA%\<appName>\agentos_kernel.db，
 * BUG-85），同一账号体系配每安装一份签名密钥=换目录重装即全端令牌作废
 * （2026-10-22 用户实测：七个安装身份=七套密钥，每次重装全量重登+已发给他
 * 机的令牌一并作废）。
 *
 * @returns 形如 %APPDATA%\<appName>\identity 的稳定目录
 */
export function resolveAuthIdentityDir(
  appDataPath: string,
  appName: string,
): string {
  return path.join(appDataPath, appName, "identity");
}

/**
 * 认证身份文件迁移种子：稳定位缺失而现役安装旧 userData 有同名文件时拷贝
 * 过去（保留现役密钥=已发令牌不作废；保留会话镜像=重装后免重登）。失败只
 * 记日志不阻断启动——迁移缺失的代价是首次启动后重新生成/重登，非致命。
 */
export function migrateIdentityFile(
  fileName: string,
  legacyDir: string,
  identityDir: string,
): void {
  const target = path.join(identityDir, fileName);
  if (fs.existsSync(target)) return;
  const source = path.join(legacyDir, fileName);
  try {
    if (!fs.existsSync(source)) return;
    fs.mkdirSync(identityDir, { recursive: true });
    fs.copyFileSync(source, target);
  } catch (err) {
    console.warn(`[Electron] 认证身份文件迁移失败: ${fileName}`, err);
  }
}

/**
 * 旧身份目录候选（认证文件迁移源，新→旧）：现役 userData 优先，其余同构
 * 哈希身份目录按目标文件 mtime 续后。换**新**目录重装时自身 userData 为空，
 * 无接管则密钥再换代一次（全端令牌再作废一次）——本机 7 身份实证换目录
 * 重装是常态而非例外（2026-10-22），迁移源必须跨身份扫描。
 */
export function legacyIdentityCandidates(
  userDataDir: string,
  fileName: string,
): string[] {
  const candidates = [userDataDir];
  try {
    const root = path.dirname(userDataDir); // ...\userdata
    const own = path.basename(userDataDir);
    candidates.push(
      ...fs
        .readdirSync(root, { withFileTypes: true })
        .filter(
          (e) =>
            e.isDirectory() && e.name !== own && /^[0-9a-f]{12}$/.test(e.name),
        )
        .map((e) => path.join(root, e.name, fileName))
        .filter((f) => fs.existsSync(f))
        .map((f) => ({ f, m: fs.statSync(f).mtimeMs }))
        .sort((a, b) => b.m - a.m)
        .map((x) => x.f),
    );
  } catch {
    // 身份根不可读：仅回退现役 userData 单源
  }
  return candidates;
}

// userData 定位必须先于抢锁：Windows 单实例锁键派生自 userData 目录
// （Chromium ProcessSingleton 的消息窗口类名与 lockfile 均以它为源），
// setPath 即改锁身份——「锁 key 含安装身份」由此成立。
// dev 不重定位（沿用默认 %APPDATA%\<productName>，与打包件天然隔离）。
if (app.isPackaged) {
  const userDataDir = resolvePackagedUserData(
    app.getPath("exe"),
    app.getPath("appData"),
    app.getName(),
  );
  app.setPath("userData", userDataDir);
  console.info(`[Electron] userData: ${userDataDir}`);
}
// 认证身份目录（打包件=机器级稳定位；dev 沿用默认 userData，与打包件天然
// 隔离——dev 内核是另一账号体系且无稳定密钥，镜像互窜会把打包件令牌喂给
// dev 内核）。迁移先于任何消费：现役安装旧 userData 内的密钥/会话镜像先
// 落稳定位，本次启动即无缝接续。
const authIdentityDir = app.isPackaged
  ? resolveAuthIdentityDir(app.getPath("appData"), app.getName())
  : app.getPath("userData");
if (app.isPackaged) {
  for (const fileName of [TOKEN_SECRET_FILE, AUTH_SESSION_FILE]) {
    for (const src of legacyIdentityCandidates(app.getPath("userData"), fileName)) {
      // migrateIdentityFile 对已存在目标幂等早退：候选序=优先级序，首中即定
      migrateIdentityFile(fileName, src, authIdentityDir);
    }
  }
}
// 允许渲染进程请求明文 http 远端（混合内容豁免）。
//
// 背景（2026-10-22 用户实测「连远端内核直接跳登录界面」的真因）：渲染页 origin
// 是 app://（registerSchemesAsPrivileged secure=true=安全源），安全源发
// http:// 属混合内容 → Chromium 直接拦掉，请求连对端都到不了；本地档因
// 127.0.0.1 属可信源而豁免，掩盖了该缺陷。远端明文连接是产品已承认的过渡态
// （连接卡有「明文过渡期」提示 + 对端内核 CORS 白名单显式放行该 origin），
// 故此处放行明文子资源；HTTPS 域名就绪后本开关可撤（连同 CORS 白名单收紧）。
//
// 必须在 app ready 前设置（Chromium 命令行开关仅 ready 前生效）。
app.commandLine?.appendSwitch?.("allow-running-insecure-content");

// 远端内核实时通道中继（2026-10-22）：app:// 源=安全源，安全源发 ws:// 远端
// 属混合内容 → 渲染层 WS 握手被浏览器拦（fetch/XHR 有 allow-running-insecure-content
// 放行，WS 不在其列）。主进程起回环 WS 端点代转接；只转发到渲染层显式声明的
// 当前内核基址（非开放代理）。内核零改动。
//
// 惰性启动（仅打包态）：dev 渲染页是 http://localhost（Vite），直连内核 WS 本就
// 可用（无混合内容约束），无需中继；测试 import main 也不应带起监听副作用。
let wsRelay: WsRelayHandle | null = null;
function ensureWsRelay(): WsRelayHandle | null {
  if (!app.isPackaged) return null;
  if (wsRelay) return wsRelay;
  wsRelay = startWsRelay();
  wsRelay.ready
    .then((p) => console.info(`[Electron] WS 中继就绪: ws://127.0.0.1:${p}/relay`))
    .catch((err) => console.warn("[Electron] WS 中继启动失败（远端实时通道不可用）:", err));
  return wsRelay;
}

// 应用壳诊断日志（观测埋点）：退出决策链（window-all-closed/before-quit/
// 内核 exit）与未捕获异常落 userData/logs/electron-main.log——打包件 console.*
// 无消费者，2026-10-10 整树退出（外部树杀 or 干净退出）无法辨别的直接缺口。
initDiagLog(path.join(app.getPath("userData"), "logs"));
logDiag(`[main] process start pid=${process.pid} isPackaged=${app.isPackaged}`);

/** webContents 所属渲染进程号（已销毁等取不到 = null；观测面不因它阻断） */
function webContentsProcessId(webContents: Electron.WebContents): number | null {
  try {
    return webContents.getProcessId();
  } catch {
    return null;
  }
}

// 渲染进程崩溃观测（证据面先行，不自动重启不弹窗；处理函数见 renderer-health.ts）。
// app 级事件覆盖全部窗口的渲染进程；进程号从事件携带的 webContents 取。
app.on(
  "render-process-gone",
  (_event, webContents, details) => {
    reportRenderProcessGone(details, webContentsProcessId(webContents), logDiag);
  },
);

// 内核日志面判活观测（消费内核 log_liveness.rs 写出的 kernel.liveness 标记；
// 模块职责见 kernel-liveness.ts）。轮询器在内核 boot 成功后起（startKernelLivenessWatch），
// 停摆/恢复跃迁经 diag-log 留痕——同一停摆态只告警一次，恢复后重新武装。
let kernelLivenessPoller: KernelLivenessPoller | null = null;

function startKernelLivenessWatch(): void {
  const logDir = managedKernelLogDir();
  const markerPath =
    logDir === null ? null : path.join(logDir, LIVENESS_MARKER_FILENAME);
  kernelLivenessPoller?.stop();
  kernelLivenessPoller = startKernelLivenessPoller({
    markerPath,
    onTransition: (snap) => {
      if (snap.markerFound && !snap.healthy) {
        const ageSecs =
          snap.lastOkWriteAgeMs === null
            ? "?"
            : String(Math.round(snap.lastOkWriteAgeMs / 1000));
        const line =
          `[kernel-liveness] 内核日志写面停摆：标记 ${ageSecs}s 未刷新` +
          `（阈值 ${KERNEL_LIVENESS_STALE_THRESHOLD_MS / 1000}s），疑似日志写者死亡`;
        console.warn("[Electron]", line);
        logDiag(line);
      } else if (snap.healthy) {
        const line = "[kernel-liveness] 内核日志写面活性恢复";
        console.info("[Electron]", line);
        logDiag(line);
      }
    },
  });
  logDiag(`[kernel-liveness] 判活轮询已启动 marker=${markerPath ?? "(路径不可知)"}`);
}

/** 当前内核日志判活快照（app:metrics 消费；轮询器未起 = 无可用标记） */
function currentKernelLiveness(): KernelLivenessSnapshot {
  return kernelLivenessPoller?.snapshot() ?? { ...KERNEL_LIVENESS_UNKNOWN };
}

// Windows toast 通知的进程身份：打包件与安装快捷方式同 AUMID；
// dev（无安装快捷方式）按 Electron 文档以 exe 路径兜底声明。
// 必须在任何 Notification 弹出之前完成（模块加载期设置一次）。
if (process.platform === "win32") {
  app.setAppUserModelId(app.isPackaged ? APP_USER_MODEL_ID : process.execPath);
}

// 禁止多实例：抢锁失败（已有首实例持有锁）即退出，否则第二实例会完整启动
// （双窗口/双托盘/共享 userData 竞争）。**必须用 app.exit 而非 app.quit**：
// 实测（.packtest/bug50b/lockprobe）模块加载期（ready 之前）的 app.quit()
// 不阻断启动序列——whenReady 照常触发、窗口/托盘/内核拉起全部照走，BUG-29
// 的旧守卫因此形同虚设（装机版双开第二实例完整启动的直接根因）；exit(0)
// 同步立即终止。第二实例此刻只执行过 setPath/协议特权注册，无受管内核、
// 无窗口托盘，无需 before-quit 清理。
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.exit(0);
}

app.on("second-instance", () => {
  focusMainWindow(mainWindow);
});

// 应用就绪后初始化
app.whenReady().then(() => {
  // 远端实时通道中继在 ready 后才起（测试环境 whenReady 不 resolve → 无监听副作用）
  ensureWsRelay();
  console.info("[Electron] 应用启动中...");

  // 生产环境移除默认应用菜单（File/Edit/View/... 由自定义标题栏取代）；
  // 开发环境保留（autoHideMenuBar 默认隐藏，Alt 唤出，DevTools/刷新快捷键可用）
  if (!isDevelopment()) {
    Menu.setApplicationMenu(null);
  }

  // 注册 app:// 协议处理器（必须先于窗口创建）
  installAppProtocolHandler();

  // 注册 IPC
  registerIpcHandlers();

  // 创建主窗口
  const win = createMainWindow();

  // 更新提示面检查（P-24，不做自动更新）：启动时读一次版本清单（环境变量
  // AGENTOS_UPDATE_MANIFEST_URL 注入；缺省禁用零网络）与新版本则推渲染层进
  // 通知中心——fire-and-forget，不阻断启动序列
  startUpdateCheck(win, { localVersion: app.getVersion() });

  // 注册全局快捷键
  registerGlobalShortcut();

  // 创建系统托盘
  createTray(win);

  console.info("[Electron] 应用启动完成");
});

// macOS 激活应用时重新创建窗口
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createMainWindow();
  } else if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.show();
  }
});

// 所有窗口关闭时退出应用（非 macOS）
app.on("window-all-closed", () => {
  logDiag("[main] window-all-closed -> app.quit()");
  if (process.platform !== "darwin") {
    app.quit();
  }
});

// 应用退出前清理资源
app.on("before-quit", () => {
  logDiag("[main] before-quit（干净退出路径开始：收内核树 + 存档）");
  // 窗口状态最后存档点：close 监听随本钩子移除，托盘退出等真退出路径
  // （不经 hide-to-tray 的 close 事件）只有这里能补存
  if (mainWindow && !mainWindow.isDestroyed()) {
    persistMainWindowState(mainWindow);
  }
  // 收受管内核进程树（taskkill /F /T 连带 sidecar；electron 崩溃路径孤儿兜底为遗留项）
  shutdownManagedKernel();
  // 移除 close 事件的 preventDefault，允许窗口真正关闭
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.removeAllListeners("close");
  }
  cleanup();
});

// 快捷键注册失败处理
app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});
