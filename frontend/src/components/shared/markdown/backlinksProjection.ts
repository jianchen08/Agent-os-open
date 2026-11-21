/**
 * 反链投影只读数据面（BacklinksPanel 与 doc_id 反查共用的单一来源）。
 *
 * 数据真值：docs/graph/backlinks.json（scripts/graph_aggregate.build_backlinks 落盘
 * 投影，含 path_to_id / id_to_path 双向）经 workspaces_file_content_get +
 * container_task_id='_local'（= 项目根）只读端点读取——零后端改动。
 */

import apiClient from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS as W } from '@/services/api/endpoints.generated'

/** 反链投影条目（与 graph_aggregate.build_backlinks 落盘契约一致） */
export interface BacklinkEntry {
  /** 源节点 id（doc_id 或 file 路径形态） */
  s: string
  /** 源节点显示名（doc = 首个标题；file = 文件名） */
  label: string
  /** 源文件仓库相对路径（条目点击跳转目标） */
  path: string
  /** 关系（开放词表：依据/取代/依赖/真值…；缺省"依据"） */
  k: string
  /** 双链第三段说明 */
  note: string
}

/** backlinks.json 投影形态（build_backlinks 返回契约） */
export interface BacklinksProjection {
  meta: { generated_at?: string; commit?: string }
  by_target: Record<string, BacklinkEntry[]>
  path_to_id: Record<string, string>
  /** 节点 id → 真实仓库路径（doc_id 形态双链点击跳转的唯一解析真值） */
  id_to_path: Record<string, string>
}

/** 投影文件在仓库内的位置（graph_aggregate 落盘契约） */
export const BACKLINKS_PATH = 'docs/graph/backlinks.json'

/** 拉取投影（_local = 项目根；端点返回 {success, content, message}） */
export async function fetchBacklinksProjection(): Promise<BacklinksProjection> {
  const resp = await apiClient.get(
    W.workspaces_file_content_get.replace('{container_task_id}', '_local'),
    { params: { path: BACKLINKS_PATH } },
  )
  const body = resp.data as { success?: boolean; content?: string; message?: string }
  if (!body?.success) {
    throw new Error(body?.message || '反向链接索引不可用')
  }
  return JSON.parse(body.content ?? '') as BacklinksProjection
}

/** doc_id → 仓库路径反查的投影缓存（成功常驻；失败即弃，下次点击重试） */
let idToPathCache: Promise<Record<string, string>> | null = null

function loadIdToPath(): Promise<Record<string, string>> {
  if (!idToPathCache) {
    idToPathCache = fetchBacklinksProjection()
      .then((projection) => projection.id_to_path ?? {})
      .catch((err: unknown) => {
        idToPathCache = null
        throw err
      })
  }
  return idToPathCache
}

/**
 * doc_id → 真实仓库路径反查（索引驱动，渲染层不发明目录约定——审查 MF-1 修复面）。
 * 命中 → 仓库相对路径；未收录（悬空引用/索引过期）→ null；
 * 投影读取失败 → 原样抛错（调用方 reportError 上报，禁静默降级）。
 */
export async function resolveDocIdPath(docId: string): Promise<string | null> {
  return (await loadIdToPath())[docId] ?? null
}
