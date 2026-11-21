/**
 * 测试装载缝：schema → 前端两张注册表（等价生产装载序 GrowthLoop）。
 *
 * C4 存量收敛（设计真值 = ADR 2026-11-04-frontend-slot-and-widget-delivery 决策 4）：
 * `contributes.pages`(space=workspace) 由 slotRegistry 翻译为 `workspace.tab`
 * keyed 槽条目，工作区页签路径直达按槽解析。只喂 contributionRegistry 的夹具会
 * 在槽房为空时解析落空——夹具统一经本入口装载，与生产同批两表。
 */
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry, type WidgetComponent } from '@/services/schema/WidgetRegistry'
import { slotRegistry, type SlotLoaderDeps } from '@/services/slots/slotRegistry'

type SchemaPayload = Parameters<typeof contributionRegistry.loadFromSchema>[0]

/** 装载 schema：页声明进 contributionRegistry，槽翻译（含 pages→workspace.tab）进 slotRegistry */
export async function loadSchemaIntoRegistries(
  payload: SchemaPayload,
  deps?: SlotLoaderDeps,
): Promise<void> {
  contributionRegistry.loadFromSchema(payload)
  await slotRegistry.loadFromSchema(payload as never, deps ?? { loadWidgets: async () => [] })
}

/** 合成 widget 名注册桩（槽装载期「声明↔注册一致性」终验需要它存在） */
export function registerWidgetStub(name: string, spaces: string[] = ['workspace']): void {
  if (widgetRegistry.has(name)) return
  widgetRegistry.register(name, (() => null) as unknown as WidgetComponent, {
    name,
    supportedSpaces: spaces as never,
  })
}
