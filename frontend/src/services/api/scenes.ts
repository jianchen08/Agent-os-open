/**
 * 场景管理 API 服务
 *
 * 提供场景 CRUD、切换和模板查询接口。
 *
 * @module api/scenes
 */

import apiClient from './client'

// ---- 类型定义 ----

/** 布局类型 */
export type SceneLayoutType = 'grid' | 'split' | 'stack' | 'tab'

/** 场景内组件配置 */
export interface SceneWidgetConfig {
  /** 组件类型 */
  widget_type: string
  /** 组件属性 */
  props: Record<string, unknown>
  /** 数据源引用 */
  data_source?: string | null
  /** 布局位置索引 */
  position: number
}

/** 场景布局配置 */
export interface SceneLayoutConfig {
  /** 布局类型 */
  type: SceneLayoutType
  /** 分割方向（仅 split） */
  direction?: string | null
  /** 网格列数（仅 grid） */
  columns?: number | null
  /** 分割比例（仅 split） */
  ratio?: number[] | null
  /** 默认标签页（仅 tab） */
  default_tab?: number | null
}

/** 场景状态快照 */
export interface SceneState {
  /** 活跃组件 ID */
  active_widget_id?: string | null
  /** 滚动位置 */
  scroll_position?: { x: number; y: number }
  /** 各组件状态 */
  widget_states?: Record<string, unknown>
  /** 自定义数据 */
  custom_data?: Record<string, unknown>
}

/** 场景数据 */
export interface Scene {
  /** 场景 ID */
  id: string
  /** 场景名称 */
  name: string
  /** 场景描述 */
  description: string
  /** 模板 ID */
  template_id?: string | null
  /** 布局配置 */
  layout: SceneLayoutConfig
  /** 组件列表 */
  widgets: SceneWidgetConfig[]
  /** 场景状态 */
  state: SceneState
  /** 是否活跃 */
  is_active: boolean
  /** 创建时间 */
  created_at: string
  /** 更新时间 */
  updated_at: string
}

/** 场景模板 */
export interface SceneTemplate {
  /** 模板 ID */
  id: string
  /** 模板名称 */
  name: string
  /** 模板描述 */
  description: string
  /** 模板图标 */
  icon: string
  /** 布局配置 */
  layout: SceneLayoutConfig
  /** 组件列表 */
  widgets: SceneWidgetConfig[]
  /** 模板分类 */
  category: string
}

/** 创建场景请求 */
export interface CreateSceneRequest {
  /** 场景名称 */
  name: string
  /** 场景描述 */
  description?: string
  /** 模板 ID */
  template_id?: string | null
  /** 布局配置 */
  layout?: SceneLayoutConfig | null
  /** 组件列表 */
  widgets?: SceneWidgetConfig[] | null
}

/** 更新场景请求 */
export interface UpdateSceneRequest {
  /** 场景名称 */
  name?: string
  /** 场景描述 */
  description?: string
  /** 布局配置 */
  layout?: SceneLayoutConfig | null
  /** 组件列表 */
  widgets?: SceneWidgetConfig[] | null
  /** 场景状态 */
  state?: SceneState | null
}

/** 列表响应 */
export interface SceneListResponse {
  items: Scene[]
  total: number
}

/** 模板列表响应 */
export interface SceneTemplateListResponse {
  items: SceneTemplate[]
  total: number
}

// ---- API 函数 ----

/**
 * 创建新场景
 *
 * @param request - 创建场景请求体
 * @returns 创建的场景数据
 */
export async function createScene(request: CreateSceneRequest): Promise<Scene> {
  const response = await apiClient.post<Scene>('/api/v1/scenes', request)
  return response.data
}

/**
 * 获取所有场景列表
 *
 * @returns 场景列表
 */
export async function listScenes(): Promise<SceneListResponse> {
  const response = await apiClient.get<SceneListResponse>('/api/v1/scenes')
  return response.data
}

/**
 * 获取场景详情
 *
 * @param sceneId - 场景 ID
 * @returns 场景数据
 */
export async function getScene(sceneId: string): Promise<Scene> {
  const response = await apiClient.get<Scene>(`/api/v1/scenes/${sceneId}`)
  return response.data
}

/**
 * 更新场景
 *
 * @param sceneId - 场景 ID
 * @param request - 更新请求体
 * @returns 更新后的场景数据
 */
export async function updateScene(
  sceneId: string,
  request: UpdateSceneRequest,
): Promise<Scene> {
  const response = await apiClient.put<Scene>(`/api/v1/scenes/${sceneId}`, request)
  return response.data
}

/**
 * 删除场景
 *
 * @param sceneId - 场景 ID
 * @returns 操作结果
 */
export async function deleteScene(
  sceneId: string,
): Promise<{ success: boolean; message: string }> {
  const response = await apiClient.delete<{ success: boolean; message: string }>(
    `/api/v1/scenes/${sceneId}`,
  )
  return response.data
}

/**
 * 切换活跃场景
 *
 * @param sceneId - 目标场景 ID
 * @returns 切换后的活跃场景
 */
export async function switchScene(sceneId: string): Promise<Scene> {
  const response = await apiClient.post<Scene>(
    `/api/v1/scenes/${sceneId}/switch`,
  )
  return response.data
}

/**
 * 获取场景模板列表
 *
 * @returns 模板列表
 */
export async function getSceneTemplates(): Promise<SceneTemplateListResponse> {
  const response = await apiClient.get<SceneTemplateListResponse>(
    '/api/v1/scenes/templates',
  )
  return response.data
}
