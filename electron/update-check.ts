/**
 * 更新检查机制（P-24 更新提示面，不做自动更新）。
 *
 * 行为契约：启动时读一次「固定版本清单位」（JSON：{version, download_url}）
 * 与本地版本三段比对；远端版本更高 → webContents.send("update:available")
 * 推给渲染层进通知中心提示「新版本可用 + 下载地址」——不下载、不安装、不弹
 * 系统通知（提示面由前端通知中心统一承载，与 P-19/P-25 形态对齐，避免双响）。
 * 同版本/本地更新/网络失败/清单不合法一律静默（检查失败不打扰用户）。
 *
 * 外部依赖假设（任务书裁定标注，2026-11-04）：
 * - 仓内打包链无更新服务器，electron-updater 自动静默更新被提案 P-24 明确
 *   否决（装机环境动作须用户确认；同名同址覆盖装流程保留人工）；
 * - 版本清单位由部署侧经环境变量 AGENTOS_UPDATE_MANIFEST_URL 注入（release/
 *   发布位自持的 JSON 清单）；**缺省禁用**——未配置即零网络请求（诚实降级，
 *   不擅自指向任何外部 URL），此时本模块完全空转、前端亦无条目；
 * - 清单格式假设为 JSON {"version": "x.y.z", "download_url": "..."}
 *   （version 语义与 electron-builder 产物 latest.yml 对齐；JSON 形态由
 *   部署侧发布时保证）。
 */

/** 新版本可用信息（推给渲染层的载荷，与 preload/electron.d.ts 契约一致） */
export interface UpdateInfo {
  /** 清单宣告的最新版本号 */
  latestVersion: string;
  /** 下载地址（清单缺 download_url 时为空串，前端不渲染地址段） */
  downloadUrl: string;
}

/** 渲染层推送通道名（preload onAvailable / electron.d.ts 同键） */
export const UPDATE_AVAILABLE_CHANNEL = "update:available";

/** 主进程 IPC 拉取通道名（registerIpcHandlers 注册 handle 用） */
export const UPDATE_GET_AVAILABLE_CHANNEL = "update:get-available";

/** 版本清单位环境变量（部署侧注入；缺省=禁用检查） */
export const UPDATE_MANIFEST_URL_ENV = "AGENTOS_UPDATE_MANIFEST_URL";

/**
 * 三段数字版本比较：a<b → 负数，a=b → 0，a>b → 正数。
 * 容忍 v 前缀；段数不齐按 0 补齐（"0.3" ≡ "0.3.0"）；逐段数字比较
 * （非字典序：0.2.10 > 0.2.9）。非数字段按 0 处理（脏数据不抛错）。
 */
export function compareVersions(a: string, b: string): number {
  const parse = (v: string): number[] =>
    v
      .trim()
      .replace(/^v/i, "")
      .split(".")
      .map((seg) => {
        const n = Number.parseInt(seg, 10);
        return Number.isNaN(n) ? 0 : n;
      });
  const pa = parse(a);
  const pb = parse(b);
  const len = Math.max(pa.length, pb.length);
  for (let i = 0; i < len; i++) {
    const sa = pa[i] ?? 0;
    const sb = pb[i] ?? 0;
    if (sa !== sb) return sa < sb ? -1 : 1;
  }
  return 0;
}

/** 检查参数（fetchFn/localVersion 可注入——测试桩；生产用默认值） */
export interface CheckOptions {
  /** 版本清单 URL（缺省读 UPDATE_MANIFEST_URL_ENV；仍未配置=禁用） */
  manifestUrl?: string;
  /** 本地版本（缺省读根 package.json 已注入的 app 版本，由调用方传入） */
  localVersion?: string;
  /** fetch 注入点（缺省 globalThis.fetch——Electron 39 主进程 Node 18+ 自带） */
  fetchFn?: typeof fetch;
}

/**
 * 拉取清单并比对版本：远端更高 → UpdateInfo；其余一律 null。
 * 网络/解析失败静默返回 null（检查是尽力而为的提示面，不产生用户可见错误）。
 */
export async function checkForNewVersion(opts: CheckOptions): Promise<UpdateInfo | null> {
  const manifestUrl = opts.manifestUrl ?? "";
  const localVersion = opts.localVersion ?? "";
  if (!manifestUrl || !localVersion) return null;
  const fetchFn = opts.fetchFn ?? globalThis.fetch.bind(globalThis);
  try {
    const resp = await fetchFn(manifestUrl);
    const manifest: unknown = await resp.json();
    if (typeof manifest !== "object" || manifest === null) return null;
    const record = manifest as { version?: unknown; download_url?: unknown };
    if (typeof record.version !== "string" || record.version.trim() === "") return null;
    const downloadUrl =
      typeof record.download_url === "string" ? record.download_url : "";
    if (compareVersions(record.version, localVersion) > 0) {
      return { latestVersion: record.version, downloadUrl };
    }
    return null;
  } catch {
    // 网络失败/非 JSON 响应：检查静默降级（无提示即无打扰），错误不上抛
    return null;
  }
}

/** 模块内最近一次检查结果（渲染层挂载晚于推送时经 IPC 拉取兜底） */
let latestUpdateInfo: UpdateInfo | null = null;

/** 渲染层拉取面（update:get-available handle 的后端） */
export function currentUpdateInfo(): UpdateInfo | null {
  return latestUpdateInfo;
}

/** 测试隔离：清状态（模块级单例，测试间互不泄漏） */
export function resetUpdateInfoForTest(): void {
  latestUpdateInfo = null;
}

/** 启动检查的目标窗口面（结构最小集；BrowserWindow 满足该形状） */
interface UpdateCheckWindow {
  webContents: { send(channel: string, payload: unknown): void };
}

/**
 * 启动时检查一次（fire-and-forget，不 await 不阻断启动序列）。
 * 版本清单位解析顺序：opts.manifestUrl → 环境变量；两者皆缺省 → 完全跳过
 * （零网络请求）。检查结果推送主窗口 + 存模块状态供拉取兜底。
 */
export function startUpdateCheck(
  win: UpdateCheckWindow,
  opts: CheckOptions = {},
): void {
  const manifestUrl = opts.manifestUrl ?? process.env[UPDATE_MANIFEST_URL_ENV] ?? "";
  if (manifestUrl === "") return;
  const localVersion = opts.localVersion ?? "";
  if (localVersion === "") return;
  void checkForNewVersion({ ...opts, manifestUrl, localVersion }).then((info) => {
    if (info === null) return;
    latestUpdateInfo = info;
    try {
      win.webContents.send(UPDATE_AVAILABLE_CHANNEL, info);
    } catch {
      // 窗口已销毁等推送失败：状态已存，渲染层挂载时可经拉取通道兜底拿到
    }
  });
}
