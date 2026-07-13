/** 执行记录查询 Hook 用于查询单个或多个执行记录，支持缓存和批量查询 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ActivityData } from '@/types/activity'
import { apiClient } from '@/services/api'

/** 执行记录数据（后端返回格式） */
export interface ExecutionRecord {
  id: string
  session_id: string
  parent_record_id?: string
  record_type: string
  executor_type?: string
  executor_id?: string
  executor_name?: string
  input_data?: Record<string, unknown>
  output_data?: Record<string, unknown>
  content?: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'
  error_message?: string
  started_at?: string
  completed_at?: string
  duration_ms?: number
  sequence?: number
  depth?: number
}

/** 执行记录缓存 */
const recordCache = new Map<string, ExecutionRecord>()

/** 将执行记录转换为活动卡片数据 */
export function executionRecordToActivity(record: ExecutionRecord): ActivityData {
  // 构建详情区块
  const details: ActivityData['details'] = []

  // 处理思考记录
  if (record.record_type === 'agent_thinking') {
    // 思考内容
    if (record.content) {
      details.push({
        id: 'thinking-content',
        label: '思考内容',
        content: record.content,
        contentType: 'text',
        collapsible: false,
        defaultExpanded: true,
      })
    }

    // 思考参数
    if (record.input_data && Object.keys(record.input_data).length > 0) {
      details.push({
        id: 'thinking-params',
        label: '思考参数',
        content: record.input_data,
        contentType: 'json',
        collapsible: true,
        defaultExpanded: false,
      })
    }
  } else {
    // 输入参数
    if (record.input_data && Object.keys(record.input_data).length > 0) {
      details.push({
        id: 'input',
        label: '输入参数',
        content: record.input_data,
        contentType: 'json',
        collapsible: true,
        defaultExpanded: false,
      })
    }

    // 输出结果
    if (record.output_data && Object.keys(record.output_data).length > 0) {
      details.push({
        id: 'output',
        label: '输出结果',
        content: record.output_data,
        contentType: 'json',
        collapsible: true,
        defaultExpanded: true,
      })
    }
  }

  // 映射状态
  const statusMap: Record<string, ActivityData['status']> = {
    pending: 'pending',
    running: 'running',
    completed: 'completed',
    failed: 'failed',
    cancelled: 'cancelled',
  }

  // 映射类型
  const typeMap: Record<string, ActivityData['type']> = {
    tool_call: 'tool_call',
    agent_response: 'custom',
    task_execution: 'task_created',
    agent_thinking: 'agent_thinking',
  }

  return {
    type: typeMap[record.record_type] || 'custom',
    id: record.id,
    title:
      record.record_type === 'agent_thinking' ? '思考过程' : record.executor_name || '执行记录',
    status: statusMap[record.status] || 'pending',
    durationMs: record.duration_ms,
    timestamp: record.started_at,
    error: record.error_message,
    details,
  }
}

/** 查询单个执行记录 */
async function fetchExecutionRecord(recordId: string): Promise<ExecutionRecord | null> {
  // 先检查缓存
  if (recordCache.has(recordId)) {
    return recordCache.get(recordId)!
  }

  try {
    const response = await apiClient.get<ExecutionRecord>(`/execution/records/${recordId}`)
    if (response.data) {
      // 缓存结果
      recordCache.set(recordId, response.data)
      return response.data
    }
    return null
  } catch (error) {
    console.error('[useExecutionRecord] 查询执行记录失败:', error)
    return null
  }
}

/** 批量查询执行记录 */
export async function fetchExecutionRecords(
  recordIds: string[],
): Promise<Map<string, ExecutionRecord>> {
  const results = new Map<string, ExecutionRecord>()
  const uncachedIds: string[] = []

  // 先从缓存获取
  for (const id of recordIds) {
    if (recordCache.has(id)) {
      results.set(id, recordCache.get(id)!)
    } else {
      uncachedIds.push(id)
    }
  }

  // 查询未缓存的记录
  if (uncachedIds.length > 0) {
    // 并行查询
    const promises = uncachedIds.map((id) => fetchExecutionRecord(id))
    const records = await Promise.all(promises)

    records.forEach((record, index) => {
      if (record) {
        results.set(uncachedIds[index], record)
      }
    })
  }

  return results
}

/** 更新缓存中的执行记录 */
export function updateRecordCache(record: ExecutionRecord): void {
  recordCache.set(record.id, record)
}

/** 清除执行记录缓存 */
export function clearRecordCache(): void {
  recordCache.clear()
}

/** 单个执行记录查询 Hook */
export function useExecutionRecord(recordId: string | null) {
  const [record, setRecord] = useState<ExecutionRecord | null>(null)
  const [activity, setActivity] = useState<ActivityData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!recordId) {
      setRecord(null)
      setActivity(null)
      return
    }

    let cancelled = false

    const load = async () => {
      setLoading(true)
      setError(null)

      try {
        const data = await fetchExecutionRecord(recordId)
        if (!cancelled) {
          setRecord(data)
          if (data) {
            setActivity(executionRecordToActivity(data))
          } else {
            setActivity(null)
          }
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : '查询失败')
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    load()

    return () => {
      cancelled = true
    }
  }, [recordId])

  /** 手动刷新 */
  const refresh = useCallback(async () => {
    if (!recordId) return

    // 清除缓存
    recordCache.delete(recordId)

    setLoading(true)
    try {
      const data = await fetchExecutionRecord(recordId)
      setRecord(data)
      if (data) {
        setActivity(executionRecordToActivity(data))
      }
    } finally {
      setLoading(false)
    }
  }, [recordId])

  return { record, activity, loading, error, refresh }
}

/** 多个执行记录查询 Hook */
export function useExecutionRecords(recordIds: string[]) {
  const [records, setRecords] = useState<Map<string, ExecutionRecord>>(new Map())
  const [activities, setActivities] = useState<Map<string, ActivityData>>(new Map())
  const [loading, setLoading] = useState(false)

  // 使用 useMemo 缓存 recordIds 的字符串表示
  const recordIdsKey = useMemo(() => recordIds.join(','), [recordIds])

  const recordIdsRef = useRef(recordIds)
  useEffect(() => { recordIdsRef.current = recordIds }, [recordIds])

  useEffect(() => {
    if (recordIdsRef.current.length === 0) {
      setRecords(new Map())
      setActivities(new Map())
      return
    }

    let cancelled = false

    const load = async () => {
      setLoading(true)

      try {
        const data = await fetchExecutionRecords(recordIdsRef.current)
        if (!cancelled) {
          setRecords(data)

          // 转换为活动数据
          const activityMap = new Map<string, ActivityData>()
          data.forEach((record, id) => {
            activityMap.set(id, executionRecordToActivity(record))
          })
          setActivities(activityMap)
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    load()

    return () => {
      cancelled = true
    }
  }, [recordIdsKey])

  return { records, activities, loading }
}

export default useExecutionRecord
