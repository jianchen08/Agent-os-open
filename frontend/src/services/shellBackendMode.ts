/**
 * 移动壳后端模式生命周期联动（remote 纯壳 / local 本机栈）。
 *
 * 两档语义与连接设置页同判：local=无用户设置（壳内置本机内核），
 * remote=用户设置指向远端。模式变更经 KernelHost.setBackendMode 落
 * SharedPreferences——原生冷启动按存储值分流（remote 不拉本地栈与前台服务，
 * 运行消耗回落纯壳档；磁盘种子保留，切回 local 即恢复离线可用），运行中切换
 * 由原生执行栈生命周期（remote 化=内核 SIGTERM 优雅停→proot 退→FGS 停；
 * local 化=重跑 bootChain，warm 路径 ~13s）。页面刷新与登录重试由前端负责
 * （连接设置页保存即重载 + 本机档免填自动登录重试窗口）。
 *
 * 浏览器/桌面无壳桥 → no-op（无本地栈生命周期可言）。
 */

import { isLoopbackOrigin } from '@/services/connectionProbe'
import { getServerBaseUrl } from '@/services/shellApiBase'
import { getShellKernelHost } from '@/services/shellPlugins'

export type ShellBackendMode = 'local' | 'remote'

/**
 * 上报后端模式给壳（保存连接地址的所有入口都必须调用，保持原生侧与
 * server_base_url 同步）。桥调用失败不阻塞保存流程：冷启动按上次存储值
 * 分流，最多一次「期望模式与运行栈不一致」，下次保存即自愈。
 */
export function applyShellBackendMode(mode: ShellBackendMode): void {
  const plugin = getShellKernelHost()
  if (!plugin?.setBackendMode) return
  void plugin.setBackendMode({ mode }).catch(() => {
    // 桥失败不重试：见上（冷启动按上次存储值分流，下次保存自愈）
  })
}

/**
 * 启动期模式自愈：持久化模式与当前连接目标归位。
 *
 * 残留病灶（2026-10-23 用户真机撞墙）：旧版把回环地址（127.0.0.1:9300）存成
 * 「自定义后端」→ 原生模式残留 remote → 本机栈不拉起 → 升级新包后 127.0.0.1
 * 永远拒连（免填凭据在、基址对、内核没起）。启动时按目标归位：
 * 本机档（无自定义基址或回环基址）→ local；非回环自定义基址 → remote。
 * 原生两侧皆幂等（bootChain 种子对账跳解压 + startStack 栈在跑则不动；
 * stopStackChain 对未运行栈为无害空转），重复调用无害。
 */
export function reconcileShellBackendMode(): void {
  const custom = getServerBaseUrl()
  const target: ShellBackendMode =
    custom !== null && !isLoopbackOrigin(custom) ? 'remote' : 'local'
  applyShellBackendMode(target)
}
