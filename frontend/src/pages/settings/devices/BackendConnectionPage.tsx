/**
 * 后端连接页（设置中枢子页）——后端连接唯一面板，设备连接与多端同步为页内区块
 * （合并改造：原独立「设备」入口退役，设置导航唯一入口=「后端连接」；
 * 承接 2026-10-18 连接/同步/我的地盘三合一的页体结构）
 *
 * 页面结构（自上而下）：
 * - 连接区：平级设备卡（本机卡=本机内核档/自定义档切换+探活；注册表设备卡
 *   在线态+卡上直连+卡内看地盘）；当前连接的卡置顶 + 高亮 + 「已连接」标记
 * - 多设备目录树浏览（本地盘+设备分支挂载锚；打开文件走 open-file）
 *
 * 入网无独立区块：设备入网 = 对方端 admin 登录自动完成（登录即入网，ADR
 * 2026-11-02-login-as-enrollment），组网动作 UI 已退役。
 */
import { PageShell } from '@/components/shared/PageShell'
import { useRelayDevicesQuery } from '@/hooks/queries/useSyncMirrorQuery'
import { DeviceDirectoryBrowser } from '@/pages/settings/devices/DeviceDirectoryBrowser'
import { PeerDeviceCards } from '@/pages/settings/devices/PeerDeviceCards'

export function BackendConnectionPage({ embedded = false }: { embedded?: boolean }) {
  const devicesQuery = useRelayDevicesQuery()

  const content = (
    <>
      <section className="mb-8">
        <h2 className="mb-1 text-sm font-semibold">连接</h2>
        <p className="text-muted-foreground mb-3 text-xs">
          本机内核档 / 自定义后端 URL 两档切换；本机与已接入设备平级，每张卡都能
          连接其内核，当前连接的卡置顶高亮。不可达时若有同步过内容，以离线快照
          呈现（不可干活）。
        </p>
        {/* ── 统一设备卡列表（注册表驱动，一机一卡；连接中的卡置顶高亮）── */}
        <div
          className="border-border flex flex-col gap-3 rounded-lg border p-4"
          data-testid="territory-list"
        >
          <PeerDeviceCards />
        </div>
        {devicesQuery.isError && (
          <p className="text-muted-foreground mt-2 text-xs">设备注册表暂不可用。</p>
        )}
      </section>
      {/* ── 多设备目录树浏览（本地盘+设备分支挂载锚；打开文件走 open-file）── */}
      <section className="mb-8">
        <DeviceDirectoryBrowser />
      </section>
    </>
  )

  if (embedded) {
    return (
      <PageShell title="后端连接" embedded>
        {content}
      </PageShell>
    )
  }
  return <PageShell title="后端连接">{content}</PageShell>
}
