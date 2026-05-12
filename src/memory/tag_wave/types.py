"""
TagWave V2 类型定义

从 V1 迁移的类型定义，保持兼容性
"""
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class TagInfo:
    """标签信息"""
    id: int
    name: str
    vector: np.ndarray
    frequency: int = 1


@dataclass
class EPAProjectionResult:
    """EPA 投影结果"""
    projections: np.ndarray  # 投影值
    probabilities: np.ndarray  # 概率分布
    entropy: float  # 归一化熵
    logic_depth: float  # 逻辑深度 (1 - entropy)
    dominant_axes: list[dict[str, Any]]  # 主导轴


@dataclass
class ResonanceResult:
    """跨域共振结果"""
    resonance: float  # 共振值
    bridges: list[dict[str, Any]]  # 桥梁连接


@dataclass
class PyramidLevel:
    """金字塔层级"""
    level: int
    tags: list[dict[str, Any]]  # 标签及其贡献度
    projection_magnitude: float
    residual_magnitude: float
    residual_energy_ratio: float
    energy_explained: float
    handshake_features: dict[str, float] | None = None


@dataclass
class PyramidResult:
    """残差金字塔分析结果"""
    levels: list[PyramidLevel]
    total_explained_energy: float
    final_residual: np.ndarray
    features: dict[str, float] = field(default_factory=dict)


@dataclass
class SearchCandidate:
    """搜索候选结果"""
    id: str
    content: str
    score: float
    vector: np.ndarray | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TagWaveConfig:
    """TagWave 配置"""
    # EPA 配置
    max_basis_dim: int = 64
    min_variance_ratio: float = 0.01
    cluster_count: int = 32
    dimension: int = 3072
    strict_orthogonalization: bool = True

    # Residual Pyramid 配置
    max_levels: int = 3
    top_k: int = 10
    min_energy_ratio: float = 0.1

    # Result Deduplicator 配置
    max_results: int = 20
    topic_count: int = 8
    redundancy_threshold: float = 0.85

    # 动态 Beta 公式参数
    alpha_min: float = 1.5
    alpha_max: float = 3.5
    beta_base: float = 2.0
    beta_range: float = 3.0

    # Tag 网络配置
    lens_top_k: int = 10
    spike_max_expand: int = 30
    min_cooccurrence: int = 1
