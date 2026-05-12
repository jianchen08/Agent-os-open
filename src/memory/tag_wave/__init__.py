"""
TagWave - 浪潮算法检索系统

基于 VCPToolBox 的 TagMemo "浪潮"算法实现

核心组件：
1. EPA 模块（嵌入投影分析）
2. 残差金字塔（多级语义残差分析）
3. 结果去重器（基于 SVD 和残差投影）
4. Tag 网络扩展（共现矩阵联想）
5. 完整检索流程
"""

from .epa_module import EPAModule
from .residual_pyramid import ResidualPyramid
from .result_deduplicator import ResultDeduplicator
from .tag_wave_retriever import TagWaveRetriever
from .types import (
    EPAProjectionResult,
    PyramidLevel,
    PyramidResult,
    ResonanceResult,
    SearchCandidate,
    TagInfo,
    TagWaveConfig,
)

__all__ = [
    # 类型定义
    'TagWaveConfig',
    'EPAProjectionResult',
    'ResonanceResult',
    'PyramidLevel',
    'PyramidResult',
    'SearchCandidate',
    'TagInfo',
    # 核心模块
    'EPAModule',
    'ResidualPyramid',
    'ResultDeduplicator',
    'TagWaveRetriever',
]
