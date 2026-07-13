/**
 * 渲染空间路由器
 *
 * 根据 Schema 的 rendering 配置，将组件映射到对应的渲染空间
 */

import type { ModuleUISchema, RenderingSpaceType } from '@/types/schema'

/** 空间路由结果 */
export interface SpaceRoute {
  space: RenderingSpaceType
  widget: string
  props: Record<string, unknown>
  dataSource?: string
  moduleId: string
}

/**
 * 从模块 Schema 中提取所有空间路由
 *
 * 遍历 Schema 的 chat 和 spaces 配置，生成路由列表
 */
export function resolveSpaceRoutes(schema: ModuleUISchema): SpaceRoute[] {
  const routes: SpaceRoute[] = []

  for (const chatConfig of schema.rendering.chat) {
    routes.push({
      space: 'chat',
      widget: chatConfig.type,
      props: chatConfig.props ?? {},
      dataSource: chatConfig.dataSource,
      moduleId: schema.identity.id,
    })
  }

  for (const spaceConfig of schema.rendering.spaces) {
    routes.push({
      space: spaceConfig.space,
      widget: spaceConfig.widget,
      props: spaceConfig.props ?? {},
      dataSource: spaceConfig.dataSource,
      moduleId: schema.identity.id,
    })
  }

  return routes
}

/**
 * 按空间类型分组路由
 *
 * 将路由列表按 chat/workspace/floating/dock/fullscreen 分组
 */
export function groupRoutesBySpace(routes: SpaceRoute[]): Record<RenderingSpaceType, SpaceRoute[]> {
  const grouped: Record<RenderingSpaceType, SpaceRoute[]> = {
    chat: [],
    workspace: [],
    floating: [],
    dock: [],
    fullscreen: [],
  }

  for (const route of routes) {
    grouped[route.space].push(route)
  }

  return grouped
}
