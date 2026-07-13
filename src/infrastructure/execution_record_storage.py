"""执行记录存储 — YAML 多文件持久化。

记录管道每轮迭代的执行详情，包括 LLM 输出摘要、工具结果摘要、
token 用量、耗时和错误信息。按 pipeline_run_id 拆分为独立 YAML 文件。

存储模式：
- 按 pipeline_run_id 拆分为独立 YAML 文件
- 目录结构：data/pipelines/{pipeline_run_id}.yaml
- 每个 YAML 文件包含 summary 和 records 两部分
- 内存缓存 + 文件持久化
- 同步 API（单管道顺序写入）
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_SUMMARY_MAX_LEN = 500
_MAX_RECORDS_PER_FILE = 500


def _record_key(record: ExecutionRecordData) -> str:
    """构造 _records dict 的组合 key：record_id::sequence。

    同一 record_id（如多轮 LLM 迭代共享 bridge message_id 的 ai 记录）的不同
    sequence 记录各自占一个 dict 槽，避免互相覆盖。record_id 字段本身保持
    与 WS message_id 一致的裸 hex（id 契约），由 sequence 区分同一逻辑消息的
    多条落盘记录。

    sequence 在管道内单调递增唯一；缺失时退化为 0（与历史脏数据兼容）。
    """
    return f"{record.record_id}::{record.sequence}"


def _fix_records_empty_flow(text: str) -> str:
    """修复 YAML 中 records: [] 后追加序列项导致的解析错误。

    旧版 _update_summary_in_file 写入 "records: []"，而 _append_record_to_file
    追加 "- record_id: ..."，两者混合产生无效 YAML。将 "records: []" 替换为
    "records:" 即可恢复正确格式。
    """
    return re.sub(r"^records:\s*\[\]\s*$", "records:", text, flags=re.MULTILINE)


@dataclass
class ExecutionRecordData:
    """L0 原始执行记录（压缩体系的 L0 层）。

    保存每个原子动作的原始消息内容，
    作为 L0→L1→L2 压缩链路的输入。

    一次 LLM 输出 = 一条 type=ai 记录（保存完整输出）
    一次工具调用 = 一条 type=tool 记录（保存输入+输出）
    """

    record_id: str = ""
    pipeline_run_id: str = ""

    type: str = "ai"
    name: str | None = None

    sequence: int = 0
    iteration: int = 0

    role: str = ""
    content: str = ""
    tool_call_id: str | None = None
    tool_input: dict[str, Any] | None = None
    thinking_content: str | None = None
    tool_calls_json: str | None = None

    container_task_id: str | None = None

    error: str | None = None

    # 前端乐观消息 ID，用于 API 历史加载时与本地临时消息对账（消除重复/丢失）
    client_message_id: str | None = None

    # 附件信息（JSON 序列化存储）
    attachments_json: str | None = None

    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.record_id:
            self.record_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


def record_role_for_llm(record: ExecutionRecordData) -> str:
    """把执行记录映射为喂给 LLM 的 role。

    与渲染路径（routes_threads._record_to_message_response 用 type 优先、
    system→system）不同：多数模型拒绝多轮穿插 system 消息，故 type=="system"
    的注入通知在此显式降级为 "user"。其余 type 按 role/type 既有映射。
    """
    if record.type == "system":
        return "user"
    _type_to_role = {"user": "user", "ai": "assistant", "tool": "tool"}
    return record.role or _type_to_role.get(record.type, "user")


@dataclass
class PipelineRunSummary:
    """管道运行摘要（每次 pipeline_run 产生一条）。

    由 PipelineEngine 在运行结束时写入，
    用于成本统计、运行日志、/cost 命令等场景。
    """

    run_id: str = ""
    thread_id: str = ""

    total_iterations: int = 0
    total_tokens: dict[str, int] = field(default_factory=dict)
    total_seconds: float = 0.0
    total_records: int = 0

    status: str = ""
    final_output: str = ""
    error: str | None = None

    review_status: str = "pending"  # "pending" 或 "reviewed"
    reviewed_at: str | None = None  # 复盘完成时间

    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


class ExecutionRecordStorage:
    """执行记录存储 — 内存缓存 + YAML 多文件持久化。

    按 pipeline_run_id 拆分为独立 YAML 文件，每个文件包含 summary 和 records。
    内存缓存按 pipeline_run_id 分组的嵌套结构。

    Attributes:
        _records: 内存中的记录缓存（record_id -> ExecutionRecordData）
        _summaries: 内存中的摘要缓存（run_id -> PipelineRunSummary）
        _pipelines: 按 pipeline_run_id 分组的管道数据（run_id -> {summary, records}）
        _data_dir: YAML 文件目录路径
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._records: dict[str, ExecutionRecordData] = {}
        self._summaries: dict[str, PipelineRunSummary] = {}
        self._loaded_pipelines: set[str] = set()
        self._data_dir = Path(data_dir) if data_dir else None
        if self._data_dir:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        self._pipeline_root_map: dict[str, str] = {}
        self._map_file = self._data_dir / "_pipeline_root_map.json" if self._data_dir else None
        if self._map_file:
            self._load_root_map()
        # pipeline_run_id -> current part number
        self._active_part: dict[str, int] = {}
        # 标记是否已通过 _load_all_summaries_only 加载过全部 summary（避免重复解析）
        self._all_summaries_loaded: bool = False
        self._records_in_active_file: dict[str, int] = {}
        # 全局 token 用量汇总缓存：避免每次刷新监控页都全量解析 11MB YAML。
        # _totals_cache: {run_id -> {input/output/cached/total}_tokens}，按 run_id 细分
        # 以便 save_summary 时只增量更新对应 run_id，无需重新汇总。
        self._totals_file = self._data_dir / "_pipeline_totals.json" if self._data_dir else None
        self._totals_cache: dict[str, dict[str, int]] = self._load_totals_cache()

    def _load_all(self) -> None:
        if not self._data_dir:
            return
        # 扁平文件（向后兼容）
        for yaml_file in sorted(self._data_dir.glob("*.yaml")):
            self._load_pipeline_file(yaml_file)
        # 子目录中的分组文件
        for subdir in sorted(self._data_dir.iterdir()):
            if not subdir.is_dir():
                continue
            for yaml_file in sorted(subdir.glob("*.yaml")):
                self._load_pipeline_file(yaml_file)

    def _ensure_loaded(self, pipeline_run_id: str) -> None:
        """按需加载指定 pipeline 的所有分片文件（懒加载）。"""
        if pipeline_run_id in self._loaded_pipelines or not self._data_dir:
            return
        part_files = self._get_part_files(pipeline_run_id)
        for pf in part_files:
            self._load_pipeline_file(pf)
        if part_files:
            self._detect_active_part(pipeline_run_id, part_files)
            active_file = part_files[-1]
            try:
                text = active_file.read_text(encoding="utf-8")
                data = yaml.safe_load(text)
                if isinstance(data, dict):
                    recs = data.get("records") or []
                    self._records_in_active_file[pipeline_run_id] = len(recs)
            except Exception:
                logger.warning(
                    "活跃分片记录数检测失败，设为 0: pipeline=%s, file=%s",
                    pipeline_run_id,
                    getattr(active_file, "name", "?"),
                )
                self._records_in_active_file[pipeline_run_id] = 0
        self._loaded_pipelines.add(pipeline_run_id)

    def _append_record_to_file(self, record: ExecutionRecordData) -> None:
        """追加单条记录到 YAML 文件末尾，避免全量重写。

        利用 YAML 序列语法：records 列表项以 '- ' 开头，
        直接在文件末尾追加一条即可被正确解析。

        分片策略：内存维护 _records_in_active_file 计数器，
        达到 _MAX_RECORDS_PER_FILE 时切换到新文件。
        """
        if not self._data_dir:
            return
        pipeline_run_id = record.pipeline_run_id
        part = self._active_part.get(pipeline_run_id, 1)
        file_path = self._get_pipeline_file(pipeline_run_id, part=part)
        if file_path is None:
            return

        record_dict = self._record_to_dict(record)
        record_yaml = yaml.safe_dump(
            [record_dict],
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
        )

        if not file_path.exists():
            file_path.parent.mkdir(parents=True, exist_ok=True)
            header = "summary: null\nrecords:\n"
            file_path.write_text(header + record_yaml, encoding="utf-8")
            self._records_in_active_file[pipeline_run_id] = 1
        else:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write("\n")
                f.write(record_yaml)
            current = self._records_in_active_file.get(pipeline_run_id, 0)
            self._records_in_active_file[pipeline_run_id] = current + 1

        if self._records_in_active_file.get(pipeline_run_id, 0) >= _MAX_RECORDS_PER_FILE:
            self._active_part[pipeline_run_id] = part + 1
            self._records_in_active_file[pipeline_run_id] = 0

    def _update_summary_in_file(self, pipeline_run_id: str) -> None:
        """文本级替换 YAML 文件中的 summary 段，避免全量重写 records。

        定位文件开头的 'summary:' 到 '\\nrecords:' 之间的内容，
        替换为最新的 summary YAML 文本。
        """
        if not self._data_dir:
            return
        part = self._active_part.get(pipeline_run_id, 1)
        file_path = self._get_pipeline_file(pipeline_run_id, part=part)
        if file_path is None:
            return

        summary = self._summaries.get(pipeline_run_id)
        summary_dict = self._summary_to_dict(summary) if summary else None
        new_summary_text = yaml.safe_dump(
            {"summary": summary_dict},
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
        ).rstrip("\n")

        if not file_path.exists():
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(
                new_summary_text + "\nrecords:\n",
                encoding="utf-8",
            )
            return

        text = file_path.read_text(encoding="utf-8")
        # 使用正则匹配文件顶层的 "records:" 行，排除 record 内容中的嵌套匹配
        _records_marker = re.search(r"^records:", text, re.MULTILINE)
        if _records_marker is None:
            logger.warning("YAML 文件格式异常，无法定位 records 段: %s", file_path.name)
            return
        marker_idx = _records_marker.start()
        # 保留 records: 及其后面的所有内容
        new_text = new_summary_text + "\n" + text[marker_idx:]
        file_path.write_text(new_text, encoding="utf-8")

    def _detect_active_part(self, pipeline_run_id: str, part_files: list[Path]) -> None:
        """从文件列表推断活跃分片编号。"""
        last = part_files[-1]
        name = last.name
        if "_" in name and name.endswith(".yaml"):
            suffix = name.rsplit("_", 1)[-1].replace(".yaml", "")
            try:
                self._active_part[pipeline_run_id] = int(suffix)
                return
            except ValueError:
                pass
        self._active_part[pipeline_run_id] = 1

    def _load_root_map(self) -> None:
        if not self._map_file or not self._map_file.exists():
            return
        try:
            text = self._map_file.read_text(encoding="utf-8")
            self._pipeline_root_map = json.loads(text)
        except Exception:
            logger.warning("管道映射文件损坏，使用空映射: %s", self._map_file)
            self._pipeline_root_map = {}

    def _load_totals_cache(self) -> dict[str, dict[str, int]]:
        """加载全局 token 用量汇总缓存文件。

        缓存格式: {pipeline_run_id: {"input_tokens": N, "output_tokens": N, ...}}。
        损坏时回退为空 dict，由后续 save_summary / get_total_tokens 重建。
        """
        if not self._totals_file or not self._totals_file.exists():
            return {}
        try:
            return json.loads(self._totals_file.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("token 用量缓存文件损坏，重建: %s", self._totals_file)
            return {}

    def _persist_totals_cache(self) -> None:
        """把 _totals_cache 落盘（原子写）。"""
        if not self._totals_file:
            return
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._totals_file.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._totals_cache, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._totals_file)
        except Exception:
            logger.warning("token 用量缓存落盘失败")

    def _ensure_totals_cached(self) -> None:
        """确保 _totals_cache 覆盖所有 summary。

        若缓存为空但磁盘有 summary 文件，说明是首次启动或缓存丢失，
        此时触发一次 summaries-only 加载重建缓存（比全量解析 records 快）。
        """
        if not self._data_dir:
            return
        # 缓存已有数据且与内存 summary 数量一致，无需重建
        if self._totals_cache and len(self._totals_cache) >= len(self._summaries):
            return
        if not self._all_summaries_loaded:
            self._load_all_summaries_only()
            self._all_summaries_loaded = True
        # 从内存 summary 重建缓存
        for summary in self._summaries.values():
            self._totals_cache[summary.run_id] = dict(summary.total_tokens)
        if self._totals_cache:
            self._persist_totals_cache()

    def _load_pipeline_file(self, yaml_file: Path) -> None:
        try:
            text = yaml_file.read_text(encoding="utf-8")
            # 修复损坏文件：将 "records: []" 替换为 "records:"
            text = _fix_records_empty_flow(text)
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                return
            summary_dict = data.get("summary")
            if summary_dict and isinstance(summary_dict, dict):
                summary = self._dict_to_summary(summary_dict)
                self._summaries[summary.run_id] = summary
            records_list = data.get("records")
            if records_list and isinstance(records_list, list):
                for record_dict in records_list:
                    if isinstance(record_dict, dict):
                        record = self._dict_to_record(record_dict)
                        self._records[_record_key(record)] = record
        except Exception:
            logger.warning("管道文件损坏，跳过: %s", yaml_file.name)

    def _load_all_summaries_only(self) -> None:
        """仅加载所有 YAML 文件的 summary 部分，跳过 records 解析。

        用于 list_all_summaries() 场景，只需要 summary 信息（如 thread_id），
        无需解析可能很大的 records 列表，显著减少内存和 CPU 开销。
        """
        if not self._data_dir:
            return
        # 扁平文件（向后兼容）
        for yaml_file in sorted(self._data_dir.glob("*.yaml")):
            self._load_summary_only(yaml_file)
        # 子目录中的分组文件
        for subdir in sorted(self._data_dir.iterdir()):
            if not subdir.is_dir():
                continue
            for yaml_file in sorted(subdir.glob("*.yaml")):
                self._load_summary_only(yaml_file)

    def _load_summary_only(self, yaml_file: Path) -> None:
        """从单个 YAML 文件中仅解析 summary 部分，跳过 records。

        关键优化：summary 段后是可能长达数 MB 的对话 records，逐行读取到
        顶格 `records:` 行即停止，再用 yaml.safe_load 只解析这一小段，
        避免把整个文件读进内存做全量解析（冷启动从 4s+ 降到亚秒级）。

        Args:
            yaml_file: YAML 文件路径
        """
        try:
            header_lines: list[str] = []
            with yaml_file.open(encoding="utf-8") as fh:
                for line in fh:
                    # 顶格 records: 是 summary 段的终点
                    if line.startswith("records:"):
                        break
                    header_lines.append(line)
                    # 安全上限：summary 段不会超过几百行，超出说明文件结构异常
                    if len(header_lines) > 2000:
                        break
            if not header_lines:
                return
            text = _fix_records_empty_flow("".join(header_lines))
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                return
            summary_dict = data.get("summary")
            if summary_dict and isinstance(summary_dict, dict):
                summary = self._dict_to_summary(summary_dict)
                self._summaries[summary.run_id] = summary
        except Exception:
            logger.warning("管道文件损坏，跳过 summary 加载: %s", yaml_file.name)

    def _get_pipeline_file(self, pipeline_run_id: str, part: int | None = None) -> Path | None:
        if not self._data_dir:
            return None
        root_id = self._pipeline_root_map.get(pipeline_run_id)
        base_dir = self._data_dir / root_id if root_id else self._data_dir
        if part is None:
            part = self._active_part.get(pipeline_run_id, 1)
        if part <= 1:
            return base_dir / f"{pipeline_run_id}.yaml"
        return base_dir / f"{pipeline_run_id}_{part:03d}.yaml"

    def _get_part_files(self, pipeline_run_id: str) -> list[Path]:
        """返回该 pipeline 所有分片文件，按编号升序。"""
        if not self._data_dir:
            return []
        root_id = self._pipeline_root_map.get(pipeline_run_id)
        base_dir = self._data_dir / root_id if root_id else self._data_dir
        if not base_dir.exists():
            return []
        files = [base_dir / f"{pipeline_run_id}.yaml"]
        files.extend(sorted(base_dir.glob(f"{pipeline_run_id}_*.yaml")))
        return [f for f in files if f.exists()]

    @staticmethod
    def _record_to_dict(record: ExecutionRecordData) -> dict[str, Any]:
        try:
            return asdict(record)
        except TypeError:
            return ExecutionRecordStorage._safe_record_to_dict(record)

    @staticmethod
    def _safe_record_to_dict(record: ExecutionRecordData) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for fld in record.__dataclass_fields__:
            val = getattr(record, fld)
            if fld == "tool_input" and isinstance(val, dict):
                result[fld] = ExecutionRecordStorage._sanitize_dict(val)
            else:
                result[fld] = val
        return result

    @staticmethod
    def _sanitize_dict(d: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for k, v in d.items():
            if isinstance(v, (str, int, float, bool, type(None))):
                result[k] = v
            elif isinstance(v, dict):
                result[k] = ExecutionRecordStorage._sanitize_dict(v)
            elif isinstance(v, list):
                result[k] = [
                    ExecutionRecordStorage._sanitize_dict(i)
                    if isinstance(i, dict)
                    else str(i)
                    if not isinstance(i, (str, int, float, bool, type(None)))
                    else i
                    for i in v
                ]
            else:
                result[k] = str(v)
        return result

    @staticmethod
    def _dict_to_record(data: dict[str, Any]) -> ExecutionRecordData:
        return ExecutionRecordData(**data)

    @staticmethod
    def _summary_to_dict(summary: PipelineRunSummary) -> dict[str, Any]:
        return asdict(summary)

    @staticmethod
    def _dict_to_summary(data: dict[str, Any]) -> PipelineRunSummary:
        return PipelineRunSummary(**data)

    def save(self, record: ExecutionRecordData) -> str:
        if not record.record_id:
            record.record_id = uuid.uuid4().hex[:12]
        if not record.created_at:
            record.created_at = datetime.now().isoformat()
        # 组合 key：record_id::sequence。同一 record_id（如多轮迭代共享 bridge
        # message_id 的 ai 记录）的不同 sequence 记录各自占一个槽，不再互相覆盖。
        # 这样 record_id 字段保持与 WS message_id 一致的裸 hex（id 契约），
        # 不再需要给多轮记录加 #iteration 后缀来强行唯一化。
        self._records[_record_key(record)] = record
        if record.pipeline_run_id:
            self._loaded_pipelines.add(record.pipeline_run_id)
            self._append_record_to_file(record)
        self._all_summaries_loaded = False
        logger.debug(
            "保存执行记录: %s (pipeline=%s, iteration=%d)", record.record_id, record.pipeline_run_id, record.iteration
        )
        return record.record_id

    def get(self, record_id: str) -> ExecutionRecordData | None:
        # 组合 key（record_id::sequence）后，record_id 不再是 dict key。
        # 全项目无调用方，保留接口向后兼容：返回该 record_id 的第一条记录。
        for r in self._records.values():
            if r.record_id == record_id:
                return r
        return None

    def list_by_session(self, session_id: str, limit: int = 50) -> list[ExecutionRecordData]:
        self._ensure_loaded(session_id)
        records = [r for r in self._records.values() if r.pipeline_run_id == session_id]
        records.sort(key=lambda r: r.iteration)
        return records[:limit]

    def count_by_session(self, session_id: str) -> int:
        self._ensure_loaded(session_id)
        return sum(1 for r in self._records.values() if r.pipeline_run_id == session_id)

    def delete_by_session(self, session_id: str) -> int:
        # 懒加载：先从磁盘读入该 pipeline 的全部记录，否则服务重启后（或会话
        # 消息从未被访问过时）self._records 为空，下面的文件清理守卫
        # `if to_delete` 会失败，导致磁盘 YAML/子目录/_pipeline_root_map 残留。
        # 与 list_by_session/count_by_session 等所有兄弟访问器保持一致。
        self._ensure_loaded(session_id)
        to_delete = [rid for rid, r in self._records.items() if r.pipeline_run_id == session_id]
        for rid in to_delete:
            del self._records[rid]
        if session_id in self._summaries:
            del self._summaries[session_id]
        self._active_part.pop(session_id, None)
        self._records_in_active_file.pop(session_id, None)
        if to_delete and self._data_dir:
            for file_path in self._get_part_files(session_id):
                file_path.unlink()
            # 清理空目录
            root_id = self._pipeline_root_map.get(session_id)
            if root_id:
                parent_dir = self._data_dir / root_id
                try:
                    if parent_dir.exists() and not any(parent_dir.iterdir()):
                        parent_dir.rmdir()
                except OSError:
                    pass
            # 清理映射
            self._pipeline_root_map.pop(session_id, None)
            self._persist_root_map()
        # 删除会话记录后重置 summary 缓存标记
        self._all_summaries_loaded = False
        logger.debug("删除会话 %s 的执行记录: %d 条", session_id, len(to_delete))
        return len(to_delete)

    def list_by_pipeline(
        self,
        pipeline_run_id: str,
        limit: int | None = None,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
    ) -> tuple[list[ExecutionRecordData], bool]:
        """
        加载指定管道的执行记录（支持游标分页）。

        FEATURE-pipeline_unify: 所有管道（主/子）统一通过 pipelineRunId 加载，
        该方法是唯一的消息加载入口，分页逻辑内联在此处。

        FEATURE-tail_read: 性能优化路径 — 传 limit 时从 YAML 文件尾部反向读取
        最近 N 条 record，避免全量反序列化 1.3MB 大文件（主管道 4-5s
        加载时间降到 0.3-0.8s）。

        调用契约:
          - limit=None: 保留原行为，全量加载所有 records（兼容 review_engine、
            reconstruct_messages 等需要完整历史的场景）。
          - limit=int:  走尾部反向读优化，只解析最近 N 条 record（适用于前端
            list_messages 翻页，主管道 4-5s → 0.3-0.8s）。

        Args:
            pipeline_run_id: 管道运行 ID
            limit: 返回的最大记录数（None 表示不限制，保留全量行为）
            before_sequence: 只返回 sequence < before_sequence 的记录（向上翻页）
            after_sequence: 只返回 sequence > after_sequence 的记录（断线补漏）

        Returns:
            (records, has_more) 元组，has_more 表示按 before_sequence 过滤后
            是否存在比 limit 更多的更早记录。
        """
        if limit is None:
            return self._list_by_pipeline_full(pipeline_run_id, before_sequence, after_sequence)

        if before_sequence is not None:
            return self._list_by_pipeline_full(pipeline_run_id, before_sequence, after_sequence, limit=limit)

        records, has_more = self.read_records_from_tail(
            pipeline_run_id,
            limit=limit,
            before_sequence=before_sequence,
            after_sequence=after_sequence,
        )
        if records:
            return records, has_more

        # 尾部读取无结果。区分两种情况：
        # - 内存模式（无 data_dir）：数据只在 _records 字典，尾部读天然读不到，
        #   必须走全量加载（内存扫描，无磁盘 IO，不慢）。
        # - 磁盘模式（有 data_dir）：尾部读读不到说明数据问题/分片损坏。
        #   不静默 fallback 到全量加载——_list_by_pipeline_full 会全量读所有分片
        #   （c7dc5433b2d7 共 5 分片 7.4MB，单次 10-40s），多 pipeline 并发即雪崩。
        #   返回空 + WARNING 暴露问题，而非被慢全量加载掩盖拖垮系统。
        if not self._data_dir:
            return self._list_by_pipeline_full(pipeline_run_id, before_sequence, after_sequence, limit=limit)

        logger.warning(
            "[list_by_pipeline] 尾部读取无结果，返回空（不 fallback 全量加载）: pipeline=%s",
            pipeline_run_id[:12],
        )
        return [], False

    def _list_by_pipeline_full(
        self,
        pipeline_run_id: str,
        before_sequence: int | None,
        after_sequence: int | None,
        limit: int | None = None,
    ) -> tuple[list[ExecutionRecordData], bool]:
        """全量加载指定管道的 records，支持游标分页和 limit 截断。

        Args:
            pipeline_run_id: 管道运行 ID
            before_sequence: 只返回 sequence < before_sequence 的记录
            after_sequence: 只返回 sequence > after_sequence 的记录
            limit: 返回的最大记录数（None 不截断）

        Returns:
            (records, has_more) 元组
        """
        self._ensure_loaded(pipeline_run_id)
        records = [r for r in self._records.values() if r.pipeline_run_id == pipeline_run_id]
        records.sort(key=lambda r: (r.sequence, r.created_at or ""))

        if after_sequence is not None:
            return [r for r in records if r.sequence > after_sequence], False

        if before_sequence is not None:
            records = [r for r in records if r.sequence < before_sequence]

        has_more = limit is not None and len(records) > limit
        if limit is not None and len(records) > limit:
            records = records[-limit:]

        return records, has_more

    def clone_pipeline_records(
        self,
        source_pipeline_id: str,
        target_pipeline_id: str,
        new_container_task_id: str,
        root_task_id: str = "",
    ) -> int:
        """全量物理拷贝源管道 records 到目标管道（文件级行替换）。

        用于 pipe 继承，避免从 messages 重建记录的格式分叉风险。
        文本级行替换 pipeline_run_id 和 container_task_id 两个字段，
        record_id 保留不动（不同管道间无全局唯一性约束）。
        替换后反序列化 YAML 验证：结构完好、字段全部替换正确。

        Args:
            source_pipeline_id: 源管道 ID
            target_pipeline_id: 目标管道 ID
            new_container_task_id: 目标管道的 container_task_id（继承者的 task_id）
            root_task_id: 目标管道的根任务 ID（用作存储目录的 root）。
                必须与 task_executor._bind_pipeline_run 的 register_pipeline(pipeline_id, root_id)
                保持一致，否则引擎注册时会触发文件迁移，导致 clone 的文件和引擎读取的文件分裂。
                为空时回退到 target_pipeline_id 自身（仅适用于无 root 绑定的场景）。

        Returns:
            拷贝的记录条数

        Raises:
            ValueError: 源管道无记录、目标文件 YAML 验证失败、字段替换不完整
        """
        src_files = self._get_part_files(source_pipeline_id)
        if not src_files:
            raise ValueError(f"源管道 {source_pipeline_id} 无执行记录，无法克隆")

        # 先读取并替换所有源文件内容（register_pipeline 前读，避免其迁移副作用干扰）
        replaced_items: list[tuple[str, str, int]] = []  # (dst_filename, serialized_yaml, part_num)
        for src_f in src_files:
            raw = src_f.read_text(encoding="utf-8")

            # YAML 解析 → 程序化改字段（比文本正则更稳健，能处理引号包裹/不同缩进等格式变体）
            data = yaml.safe_load(raw)
            if not isinstance(data, dict):
                raise ValueError(f"克隆管道记录失败：源文件 {src_f.name} 格式无效")
            records = data.get("records")
            if not records or not isinstance(records, list):
                raise ValueError(f"克隆管道记录失败：源文件 {src_f.name} records 为空或格式无效")

            # 诊断日志：记录源信息
            first_pid = records[0].get("pipeline_run_id", "(空)") if records else "(空)"
            logger.info(
                "克隆源文件 | src_file=%s | src_pid=%s | 首条记录pid=%s | 记录数=%d",
                src_f,
                source_pipeline_id,
                first_pid,
                len(records),
            )

            # 逐条替换：pipeline_run_id / container_task_id 改为目标值，
            # record_id 保留不动（clone 忠实复制源数据，源内重复由 track plugin 落盘环节负责）。
            for rec in records:
                rec["pipeline_run_id"] = target_pipeline_id
                rec["container_task_id"] = new_container_task_id

            # 序列化回 YAML（保持与源文件一致的序列化风格）
            # 注：safe_dump 输出与原始 YAML 的缩进/换行等细节可能不完全相同，
            # 但数据结构完全等价，后续 list_by_pipeline 通过 yaml.safe_load 读取，
            # 对序列化细节无依赖。
            replaced = yaml.safe_dump(
                data,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                indent=2,
            )

            dst_name = src_f.name.replace(source_pipeline_id, target_pipeline_id)
            part_num = 1
            stem = src_f.stem
            if "_" in stem:
                try:
                    part_num = int(stem.rsplit("_", 1)[-1])
                except (ValueError, IndexError):
                    part_num = 1
            replaced_items.append((dst_name, replaced, part_num))

        # 注册目标 pipeline 到存储目录，root 必须与 _bind_pipeline_run 一致，
        # 否则引擎注册时 register_pipeline(pipeline_id, root_id) 会触发文件迁移，
        # 导致 clone 的文件和引擎读取的文件分裂（继承历史丢失）。
        _root = root_task_id or target_pipeline_id
        self.register_pipeline(target_pipeline_id, _root)
        base_dir = self._data_dir / _root
        base_dir.mkdir(parents=True, exist_ok=True)

        written_files: list[Path] = []  # 已写入的目标文件（失败时回滚用）
        try:
            total = 0
            for dst_name, replaced, part_num in replaced_items:
                dst_f = base_dir / dst_name
                dst_f.write_text(replaced, encoding="utf-8")
                written_files.append(dst_f)

                # YAML 验证：结构完好 + 字段全部替换正确
                try:
                    data = yaml.safe_load(replaced)
                except yaml.YAMLError as e:
                    raise ValueError(f"克隆管道记录失败：目标文件 {dst_name} YAML 解析错误 - {e}") from e
                records = data.get("records") if isinstance(data, dict) else None
                if not records:
                    raise ValueError(f"克隆管道记录失败：目标文件 {dst_name} 的 records 为空或格式异常")
                for i, rec in enumerate(records):
                    rec_pid = rec.get("pipeline_run_id")
                    rec_ctid = rec.get("container_task_id")
                    if rec_pid != target_pipeline_id:
                        raise ValueError(
                            f"克隆管道记录验证失败：记录 {i} 的 pipeline_run_id "
                            f"仍为 '{rec_pid}'，应替换为 '{target_pipeline_id}'"
                        )
                    if rec_ctid != new_container_task_id:
                        raise ValueError(
                            f"克隆管道记录验证失败：记录 {i} 的 container_task_id "
                            f"仍为 '{rec_ctid}'，应替换为 '{new_container_task_id}'"
                        )

                # 更新分片状态
                self._active_part[target_pipeline_id] = part_num
                self._records_in_active_file[target_pipeline_id] = len(records)
                total += len(records)
        except Exception:
            # 回滚：删除已写入的目标文件、空目录、root_map 映射，
            # 避免失败后残留垃圾文件污染后续重试/继承
            self._rollback_clone(target_pipeline_id, written_files, base_dir)
            raise

        logger.info(
            "克隆管道记录完成 | src=%s dst=%s records=%d",
            source_pipeline_id[:12],
            target_pipeline_id[:12],
            total,
        )
        return total

    def _rollback_clone(
        self,
        target_pipeline_id: str,
        written_files: list[Path],
        base_dir: Path,
    ) -> None:
        """clone 失败时回滚：删除已写入文件、空目录、root_map 映射。"""
        for f in written_files:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        # 删除目标目录（仅当为空时，避免误删其他内容）
        try:
            if base_dir.exists() and not any(base_dir.iterdir()):
                base_dir.rmdir()
        except OSError:
            pass
        # 清理 root_map 映射并持久化
        if self._pipeline_root_map.pop(target_pipeline_id, None) is not None:
            self._persist_root_map()
        # 清理内存分片状态
        self._active_part.pop(target_pipeline_id, None)
        self._records_in_active_file.pop(target_pipeline_id, None)
        logger.warning(
            "克隆管道记录已回滚 | dst=%s | 删除文件=%d",
            target_pipeline_id[:12],
            len(written_files),
        )

    def reconstruct_messages(
        self,
        pipeline_run_id: str,
        budget: int | None = None,
        token_fn: Callable[..., int] | None = None,
    ) -> list[dict[str, Any]]:
        """从 L0 持久化记录回读近期消息（从后往前，按预算截取）。

        惰性加载：只读取当前活跃分片文件，预算不够时才往前读更早的分片。

        Args:
            pipeline_run_id: 管道运行 ID
            budget: token 预算限制（None 表示无限制）
            token_fn: token 估算函数，默认 len(text)//2

        Returns:
            消息字典列表（按时间顺序，旧的在前）
        """
        if token_fn is None:

            def _default_token_fn(text: str) -> int:
                return max(1, len(text) // 2) if text else 0

            token_fn = _default_token_fn

        # 内存回退：无 data_dir 时直接从缓存读取
        all_records = [r for r in self._records.values() if r.pipeline_run_id == pipeline_run_id]
        all_records.sort(key=lambda r: (r.sequence, r.created_at or ""))

        if not all_records:
            # 磁盘回读：从分片文件倒序加载
            for pf in reversed(self._get_part_files(pipeline_run_id)):
                part_records = self._load_part_records(pf)
                all_records.extend(part_records)
                if budget is not None:
                    # 按预算判断是否需要继续读更早的分片
                    total = sum(token_fn(r.content or "") for r in all_records)
                    if total >= budget:
                        break
            all_records.sort(key=lambda r: (r.sequence, r.created_at or ""))

        if not all_records:
            return []

        return self._select_within_budget(all_records, budget, token_fn)

    def _select_within_budget(
        self,
        records: list[ExecutionRecordData],
        budget: int | None,
        token_fn: Callable[..., int],
    ) -> list[dict[str, Any]]:
        """从已排序的记录中，从后往前按预算截取消息。"""
        selected: list[ExecutionRecordData] = []
        used_tokens = 0
        pending_tools: list[ExecutionRecordData] = []

        for record in reversed(records):
            if record.type == "compression_marker":
                continue

            if record.type == "tool":
                pending_tools.append(record)
                continue

            rec_tokens = token_fn(record.content or "")
            if record.type == "ai" and record.tool_calls_json:
                rec_tokens += token_fn(record.tool_calls_json)

            tool_tokens = sum(token_fn(r.content or "") for r in pending_tools)
            total_tokens = rec_tokens + tool_tokens

            if budget is not None and used_tokens + total_tokens > budget:
                pending_tools.clear()
                break

            if pending_tools:
                selected.extend(pending_tools)
                used_tokens += tool_tokens
                pending_tools.clear()
            selected.append(record)
            used_tokens += rec_tokens

        pending_tools.clear()
        selected.reverse()
        return [self._record_to_message(r) for r in selected]

    def _load_part_records(self, file_path: Path) -> list[ExecutionRecordData]:
        """加载单个分片文件的记录（不更新全局缓存）。"""
        try:
            text = file_path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                return []
            records_list = data.get("records", [])
            if not records_list or not isinstance(records_list, list):
                return []
            return [self._dict_to_record(d) for d in records_list if isinstance(d, dict)]
        except Exception as exc:
            logger.warning("分片文件损坏，跳过加载: %s - %s", file_path.name, exc)
            return []

    # 尾部读取窗口大小：128KB 起始；不够时每次再补 128KB（补充循环），
    # 直到凑够需要的条数或覆盖整个文件（按平均 2KB/record 估算，128KB 约 60 条）。
    _TAIL_READ_BYTES = 128 * 1024

    def _extract_tail_blocks(self, yaml_file: Path, n: int) -> list[str]:
        """从单个 YAML 分片文件尾部提取最后 n 个 record 的文本块（不解 YAML）。

        FEATURE-tail_read:
        算法说明:
          1. 读取文件末尾字节窗口（起始 128KB），反序列化量从全文件降到 KB 级。
          2. 在窗口内按 "\n- " 切分序列项起点（continuation 行是缩进，不含 "- "），
             取最后 n 个起点作为 record 块边界。
          3. 若窗口内 - 起点少于 n 个（边界不够），每次再补 128KB 继续向前扩展，
             直到凑够 n 个起点或已覆盖整个文件。

        YAML 序列项结构:
          records:
          - record_id: r001
            field: value
          - record_id: r002
            field: value

        切分时 "\n- " 只匹配真正的 record 起始位置（continuation 行无 "- "），
        不会切到 record 内部的字段值。

        Args:
            yaml_file: 单个分片 YAML 文件路径
            n: 期望提取的 record 块数（可能被文件实际数量截断）

        Returns:
            record 文本块列表（按文件中出现的顺序，每个块以 "- " 开头）
        """
        if n <= 0:
            return []
        try:
            file_size = yaml_file.stat().st_size
        except OSError:
            return []
        if file_size == 0:
            return []

        pattern = "\n- "
        # 窗口每次必须用 min(step, file_size) 限制，
        # 避免小文件（<窗口大小）下 seek 越过文件起始位置触发 OSError。
        step = self._TAIL_READ_BYTES
        window = min(step, file_size)
        blocks: list[str] = []

        # 补充循环：起始窗口 128KB，不够就每次再向前扩 128KB，
        # 直到凑够 n 个 record 起点、或已覆盖整个文件为止。
        while True:
            try:
                with open(yaml_file, "rb") as f:
                    f.seek(file_size - window)
                    tail_bytes = f.read()
            except OSError:
                return blocks
            tail_text = tail_bytes.decode("utf-8", errors="replace")

            indices = [i for i in range(len(tail_text)) if tail_text.startswith(pattern, i)]
            # 满足任一条件则返回已找到的块：
            # 1) 已找到足够多的 record 起点；
            # 2) 本次窗口已覆盖整个文件（读不到更多）。
            enough = len(indices) >= n
            full_file_covered = window >= file_size
            if enough or full_file_covered:
                take = min(n, len(indices))
                start_indices = indices[-take:] if take > 0 else []
                for i, start in enumerate(start_indices):
                    block_start = start + 1  # 跳过 \n，保留 "- "
                    block_end = start_indices[i + 1] if i + 1 < len(start_indices) else len(tail_text)
                    block = tail_text[block_start:block_end].rstrip()
                    if block:
                        blocks.append(block)
                return blocks

            # 不够且文件尚未读完：窗口再扩 128KB 继续
            if window >= file_size:
                return blocks
            window = min(window + step, file_size)

    def read_records_from_tail(  # noqa: PLR0911,PLR0912
        self,
        pipeline_run_id: str,
        limit: int,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
    ) -> tuple[list[ExecutionRecordData], bool]:
        """从 YAML 分片文件尾部反向读取 records（不加载整个文件）。

        FEATURE-tail_read:
        性能优化: 主管道单文件 1.3MB / 500 条 record 时，全量 yaml.safe_load
        需 4-5s；本方法只读末尾字节窗口（起始 128KB，不够再补 128KB），
        仅解析需要的 N 条 record，加载时间降到 0.3-0.8s（5-10x 提升）。

        算法:
          1. 倒序遍历所有分片文件（最新的分片 part 编号最大），
             从每个分片尾部提取 N 个 record 文本块。
          2. 跨分片累积直到凑够 limit 条（或所有分片读完）。
          3. 拼装为最小 YAML 文档（records:\\n + 文本块）后 safe_load。
          4. 按游标（before/after sequence）过滤并截断 limit。

        Args:
            pipeline_run_id: 管道运行 ID
            limit: 最多返回的 records 数（断线补漏场景下不截断）
            before_sequence: 只返回 sequence < before_sequence 的 records
            after_sequence: 只返回 sequence > after_sequence 的 records（断线补漏）

        Returns:
            (records, has_more) - records 按 sequence 升序；has_more 表示
            在 before_sequence 边界内是否还有更多未读取的更早 records。

        边界处理:
          - 无 _data_dir / 无分片文件: 返回 ([], False)
          - 解析失败: 返回 ([], False)，由调用方 fallback 到全量加载
          - 末尾分片为空: 自动读上一个分片
        """
        if not self._data_dir:
            return [], False

        part_files = sorted(self._get_part_files(pipeline_run_id), reverse=True)
        if not part_files:
            return [], False

        collected_blocks: list[str] = []
        has_more = False
        # 单分片读取上限：单分片最多 _MAX_RECORDS_PER_FILE 条 record，
        # 断线补漏场景下需要把所有新 record 都捞回来，不能用 limit 截断
        per_part_cap = _MAX_RECORDS_PER_FILE

        for part_file in part_files:
            if after_sequence is not None:
                # 断线补漏：只收集可能含 after_sequence 之后新 record 的分片。
                # 为避免对每个分片都提取 _MAX_RECORDS_PER_FILE(500) 条、遍历所有分片
                # 造成全量读取（2.7MB，单次请求 3-4s，用户感知"刷新加载慢"），
                # 利用 sequence 在分片间单调递增（实测 _004=1941-2210 > _003=1290-1940 > ...）
                # 的特性：after_sequence 之后的新 record 只可能在最新 1-2 个分片。
                # 因此从最新分片倒序读，一旦某分片所有 sequence 都 ≤ after_sequence，
                # 更早分片更小 → 停止（不再读历史分片）。
                blocks = self._extract_tail_blocks(part_file, per_part_cap)
                if not blocks:
                    continue
                # 判断该分片是否含 > after_sequence 的 record：
                # 解析 blocks 中所有 sequence，看是否有大于 after_sequence 的。
                # blocks 是 YAML 文本块，这里用轻量正则提取 sequence 字段。
                import re as _re  # noqa: PLC0415

                block_text = "\n".join(blocks)
                seqs = [int(m) for m in _re.findall(r"sequence:\s*(\d+)", block_text)]
                part_has_new = any(s > after_sequence for s in seqs)
                if part_has_new:
                    collected_blocks.extend(blocks)
                else:
                    # 该分片无新 record，更早分片 sequence 更小，不会有新 record → 停止
                    break
            # 初始加载 / 向上翻页：从尾部读，按需扩大
            needed = limit - len(collected_blocks)
            if needed <= 0:
                has_more = True
                break
            # _extract_tail_blocks 内部已会补充循环扩展窗口，
            # 直到凑够 needed 或覆盖整个文件，此处不再需要窗口上限判断。
            blocks = self._extract_tail_blocks(part_file, needed)
            collected_blocks.extend(blocks)
            # 当前分片已提供足够块，说明更早分片可能还有更早 record
            if len(blocks) >= needed:
                has_more = True
                break
            # 当前分片整文件都不够 needed：继续读更早分片补齐；
            # 若已是最后一个分片，循环正常结束，has_more 保持 False。

        if not collected_blocks:
            return [], False

        yaml_text = "records:\n" + "\n".join(collected_blocks)
        try:
            data = yaml.safe_load(yaml_text)
        except Exception as exc:
            logger.warning("反向读取 YAML 解析失败: %s - %s", pipeline_run_id, exc)
            return [], False

        raw_records = data.get("records") if isinstance(data, dict) else []
        if not isinstance(raw_records, list) or not raw_records:
            return [], False

        records = [self._dict_to_record(rd) for rd in raw_records if isinstance(rd, dict)]
        records.sort(key=lambda r: (r.sequence, r.created_at or ""))

        # 断线补漏：只过滤，不截断
        if after_sequence is not None:
            return [r for r in records if r.sequence > after_sequence], False

        if before_sequence is not None:
            records = [r for r in records if r.sequence < before_sequence]

        # 截断到 limit 条（保留最新的），has_more 保留收集循环的判断
        if limit is not None and len(records) > limit:
            records = records[-limit:]

        return records, has_more

    @staticmethod
    def _record_to_message(record: ExecutionRecordData) -> dict[str, Any]:
        """将 ExecutionRecordData 转换为 message dict 格式。"""
        # 喂给 LLM 的 role：type==system 的注入通知降级为 user（见 record_role_for_llm）
        role = record_role_for_llm(record)
        msg: dict[str, Any] = {
            "role": role,
            "content": record.content or "",
        }

        # 恢复 tool_calls
        if record.type == "ai" and record.tool_calls_json:
            try:
                tool_calls = json.loads(record.tool_calls_json)
                if tool_calls:
                    msg["tool_calls"] = tool_calls
            except (json.JSONDecodeError, TypeError):
                pass

        # 恢复 tool_call_id
        if record.type == "tool" and record.tool_call_id:
            msg["tool_call_id"] = record.tool_call_id

        return msg

    def save_summary(self, summary: PipelineRunSummary) -> str:
        if not summary.run_id:
            summary.run_id = uuid.uuid4().hex[:12]
        if not summary.created_at:
            summary.created_at = datetime.now().isoformat()
        self._summaries[summary.run_id] = summary
        self._loaded_pipelines.add(summary.run_id)
        self._update_summary_in_file(summary.run_id)
        self._all_summaries_loaded = False
        # 增量更新全局 token 用量缓存：新管道加入、已有管道覆盖最新值
        self._totals_cache[summary.run_id] = dict(summary.total_tokens)
        self._persist_totals_cache()
        logger.debug(
            "保存管道摘要: %s (iterations=%d, status=%s)", summary.run_id, summary.total_iterations, summary.status
        )
        return summary.run_id

    def register_pipeline(
        self,
        pipeline_run_id: str,
        root_task_id: str,
    ) -> None:
        old_root = self._pipeline_root_map.get(pipeline_run_id)
        if old_root == root_task_id:
            return
        self._pipeline_root_map[pipeline_run_id] = root_task_id
        self._persist_root_map()
        # 如果扁平位置有文件，迁移到子目录
        if self._data_dir:
            flat_path = self._data_dir / f"{pipeline_run_id}.yaml"
            if flat_path.exists():
                target_dir = self._data_dir / root_task_id
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = target_dir / f"{pipeline_run_id}.yaml"
                flat_path.rename(target_path)
                logger.info("迁移管道文件: %s -> %s", flat_path.name, root_task_id)
            # 也迁移分片文件
            for flat_part in self._data_dir.glob(f"{pipeline_run_id}_*.yaml"):
                target_dir = self._data_dir / root_task_id
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = target_dir / flat_part.name
                flat_part.rename(target_path)
            self._active_part.pop(pipeline_run_id, None)

    def _persist_root_map(self) -> None:
        if not self._data_dir or not self._map_file:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._map_file.write_text(
            json.dumps(self._pipeline_root_map, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def get_summary(self, run_id: str) -> PipelineRunSummary | None:
        self._ensure_loaded(run_id)
        return self._summaries.get(run_id)

    def update_summary(self, pipeline_run_id: str, updates: dict[str, Any]) -> None:
        """更新指定管道运行的 summary 字段并持久化到磁盘。

        将 thread_id 等关联信息写入 summary。

        Args:
            pipeline_run_id: 管道运行 ID
            updates: 需要更新的字段字典，例如 {"thread_id": "abc123"}
        """
        self._ensure_loaded(pipeline_run_id)
        summary = self._summaries.get(pipeline_run_id)
        if summary is None:
            # summary 尚不存在，创建一个空 summary 再更新
            summary = PipelineRunSummary(run_id=pipeline_run_id)
            self._summaries[pipeline_run_id] = summary
        for key, value in updates.items():
            if hasattr(summary, key):
                setattr(summary, key, value)
        self._update_summary_in_file(pipeline_run_id)
        self._all_summaries_loaded = False
        logger.debug(
            "更新管道摘要字段: %s (updates=%s)",
            pipeline_run_id,
            list(updates.keys()),
        )

    def list_all_summaries(self) -> list[PipelineRunSummary]:
        """返回所有已加载的管道运行摘要列表（不做数量限制）。

        根据 summary.thread_id 反查属于某个 thread 的所有 pipeline_run_id。

        性能优化: 使用 _load_all_summaries_only 仅加载 summary 部分，
        并通过 _all_summaries_loaded 标记避免重复解析。

        Returns:
            全部 PipelineRunSummary 列表
        """
        if self._data_dir and not self._all_summaries_loaded:
            self._load_all_summaries_only()
            self._all_summaries_loaded = True
        return list(self._summaries.values())

    def list_summaries(self, limit: int = 50) -> list[PipelineRunSummary]:
        # summaries 需要全量扫描，触发一次性加载
        if self._data_dir and not self._loaded_pipelines:
            self._load_all()
        summaries = sorted(
            self._summaries.values(),
            key=lambda s: s.created_at,
            reverse=True,
        )
        return summaries[:limit]

    def get_total_tokens(self) -> dict[str, int]:
        """汇总所有管道的累计 token 用量。

        优先读 _totals_cache（持久化 + 内存），命中则亚秒级返回；
        缓存缺失时触发 summaries-only 加载重建（不解析 records）。
        """
        self._ensure_totals_cached()
        total: dict[str, int] = {}
        for tokens in self._totals_cache.values():
            for key, value in tokens.items():
                total[key] = total.get(key, 0) + value
        return total


def summarize_text(text: Any, max_len: int = 500) -> str:
    """截断长文本用于摘要显示。"""
    if text is None:
        return ""
    s = str(text)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...(truncated)"
