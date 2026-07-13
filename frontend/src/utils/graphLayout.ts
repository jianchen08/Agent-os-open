/**
 * 图布局工具函数
 * 用于计算和优化节点在图中的位置
 */

import type { Node, Edge } from '@xyflow/react'

export interface LayoutOptions {
  direction?: 'TB' | 'BT' | 'LR' | 'RL'
  spacing?: [number, number]
  nodeWidth?: number
  nodeHeight?: number
}

const DEFAULT_OPTIONS: Required<LayoutOptions> = {
  direction: 'TB',
  spacing: [100, 100],
  nodeWidth: 200,
  nodeHeight: 80,
}

/**
 * 应用分层布局到图数据
 */
export function applyLayeredLayout(
  nodes: Node[],
  edges: Edge[],
  options: LayoutOptions = {},
): { nodes: Node[]; edges: Edge[] } {
  const opts = { ...DEFAULT_OPTIONS, ...options }

  // 简单的分层布局算法
  const layers = buildLayers(nodes, edges)
  const positionedNodes = positionNodes(layers, opts)

  return {
    nodes: positionedNodes,
    edges,
  }
}

/**
 * 构建节点层级
 */
function buildLayers(nodes: Node[], edges: Edge[]): Node[][] {
  const layers: Node[][] = []
  const visited = new Set<string>()
  const nodeMap = new Map(nodes.map((n) => [n.id, n]))

  // 找到根节点（没有入边的节点）
  const rootNodeIds = new Set(
    nodes.map((n) => n.id).filter((id) => !edges.some((e) => e.target === id)),
  )

  // 从根节点开始 BFS 分层
  let currentLayer: Node[] = []

  for (const rootId of rootNodeIds) {
    const rootNode = nodeMap.get(rootId)
    if (rootNode && !visited.has(rootId)) {
      currentLayer.push(rootNode)
      visited.add(rootId)
    }
  }

  while (currentLayer.length > 0) {
    layers.push(currentLayer)
    const nextLayer: Node[] = []

    for (const node of currentLayer) {
      const children = edges
        .filter((e) => e.source === node.id)
        .map((e) => nodeMap.get(e.target))
        .filter((n): n is Node => n !== undefined && !visited.has(n.id))

      for (const child of children) {
        if (!visited.has(child.id)) {
          nextLayer.push(child)
          visited.add(child.id)
        }
      }
    }

    currentLayer = nextLayer
  }

  // 添加未访问的节点（孤立节点）
  const unvisitedNodes = nodes.filter((n) => !visited.has(n.id))
  if (unvisitedNodes.length > 0) {
    layers.push(unvisitedNodes)
  }

  return layers
}

/**
 * 为节点计算位置
 */
function positionNodes(layers: Node[][], options: Required<LayoutOptions>): Node[] {
  const [spacingX, spacingY] = options.spacing
  const positionedNodes: Node[] = []

  layers.forEach((layer, layerIndex) => {
    const layerWidth = layer.length * options.nodeWidth + (layer.length - 1) * spacingX
    const startX = -layerWidth / 2

    layer.forEach((node, nodeIndex) => {
      positionedNodes.push({
        ...node,
        position: {
          x: startX + nodeIndex * (options.nodeWidth + spacingX),
          y: layerIndex * (options.nodeHeight + spacingY),
        },
      })
    })
  })

  return positionedNodes
}

/**
 * 计算节点中心位置
 */
export function getNodeCenter(node: Node): { x: number; y: number } {
  const width = (node.style?.width ?? 200) as number
  const height = (node.style?.height ?? 80) as number

  return {
    x: node.position.x + width / 2,
    y: node.position.y + height / 2,
  }
}

/**
 * 检测节点是否重叠
 */
export function hasOverlap(node1: Node, node2: Node): boolean {
  const center1 = getNodeCenter(node1)
  const center2 = getNodeCenter(node2)
  const width1 = (node1.style?.width ?? 200) as number
  const height1 = (node1.style?.height ?? 80) as number
  const width2 = (node2.style?.width ?? 200) as number
  const height2 = (node2.style?.height ?? 80) as number

  return (
    Math.abs(center1.x - center2.x) < (width1 + width2) / 2 &&
    Math.abs(center1.y - center2.y) < (height1 + height2) / 2
  )
}

/**
 * 获取布局后的元素（兼容旧代码）
 */
export function getLayoutedElements(nodes: Node[], edges: Edge[]): Node[] {
  const { nodes: layoutedNodes } = applyLayeredLayout(nodes, edges)
  return layoutedNodes
}
