"""经验轨迹纯函数库（无 I/O，便于单测）。

职责：把内核 traces + 复盘报告折算成 `agentos.experience.v1` episode。
不读库、不写盘、不调用 LLM；所有 I/O 在 experience_build.py / learning_curve.py。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SUCCESS_STATUSES = {"completed", "done", "success"}
REVIEW_SCORE_MIN = -10
REVIEW_SCORE_MAX = 3
MAX_GOAL_CHARS = 2000
MAX_NORMALIZED_ACTION_CHARS = 240

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")
_PATH_KEYS = {"path", "file", "file_path", "cwd", "workdir", "workspace", "target_path"}
_VOLATILE_KEYS = {"timestamp", "created_at", "updated_at", "session_id", "pipeline_id", "user_id"}


def decode_state_value(value: Any) -> Any:
    """state 值 → Python 对象：JSON 优先，失败按原字符串返回。"""
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def tokenize(text: str) -> set[str]:
    """检索用 token 集：英文/数字词 + 单个汉字。"""
    return set(_TOKEN_RE.findall((text or "").lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度；双方为空返回 0。"""
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _normalize_path(value: str, workspace: str | None) -> str:
    text = str(value).replace("\\", "/").strip()
    if workspace:
        ws = str(workspace).replace("\\", "/").rstrip("/")
        if ws and text.lower().startswith(ws.lower()):
            text = text[len(ws):]
            text = text.lstrip("/")
    text = re.sub(r"/+", "/", text)
    if text.startswith("./"):
        text = text[2:]
    return text or "."


def _normalize_arg_value(key: str, value: Any, workspace: str | None) -> Any:
    if isinstance(value, str) and key in _PATH_KEYS:
        return _normalize_path(value, workspace)
    if isinstance(value, dict):
        return {k: _normalize_arg_value(k, v, workspace) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize_arg_value(key, v, workspace) for v in value]
    return value


def normalize_action(raw: str | None = None, tool: str | None = None,
                     args: dict[str, Any] | None = None,
                     workspace: str | None = None) -> dict[str, Any]:
    """动作归一：工具名 + 规范化参数，生成跨 episode 可复用的键。

    `raw` 可为 JSON 字符串、`tool({...})` 文本或普通命令；解析失败时保留原文。
    """
    parsed_args = args
    if parsed_args is None and raw:
        text = str(raw).strip()
        if text.startswith("{"):
            try:
                parsed_args = json.loads(text)
            except ValueError:
                parsed_args = None
        if parsed_args is None:
            match = re.match(r"^([A-Za-z_][\w.\-]*)\((.*)\)$", text, re.S)
            if match:
                tool = tool or match.group(1)
                try:
                    parsed_args = json.loads(match.group(2))
                except ValueError:
                    parsed_args = None
    if isinstance(parsed_args, str):
        try:
            parsed_args = json.loads(parsed_args)
        except ValueError:
            parsed_args = None

    tool_name = (tool or "unknown").strip() or "unknown"
    clean_args: dict[str, Any] = {}
    if isinstance(parsed_args, dict):
        for key, value in sorted(parsed_args.items()):
            if key in _VOLATILE_KEYS:
                continue
            clean_args[key] = _normalize_arg_value(key, value, workspace)

    if clean_args:
        parts = [f"{k}={json.dumps(v, ensure_ascii=False, sort_keys=True)}"
                 for k, v in clean_args.items()]
        normalized = f"{tool_name}({','.join(parts)})"
    else:
        fallback = re.sub(r"\s+", " ", str(raw or "")).strip()
        normalized = f"{tool_name}({fallback})" if fallback else f"{tool_name}()"
    if len(normalized) > MAX_NORMALIZED_ACTION_CHARS:
        normalized = normalized[:MAX_NORMALIZED_ACTION_CHARS - 1] + "…"

    return {
        "raw": str(raw or ""),
        "normalized": normalized,
        "tool": tool_name,
        "args": clean_args,
    }


def make_state(goal: str, step: int, last_observation: str | None,
               trace_seq: int | None) -> dict[str, Any]:
    """压缩状态表示：目标摘要 + 步号 + 上一步观测摘要 + trace 锚。"""
    goal_text = (goal or "")[:120]
    obs = (last_observation or "none")[:120]
    repr_text = f"goal={goal_text}|step={step}|last={obs}|seq={trace_seq}"
    return {
        "repr": repr_text,
        "digest": "sha256:" + hashlib.sha256(repr_text.encode("utf-8")).hexdigest(),
        "tokens": len(tokenize(repr_text)),
    }


def discounted_returns(scores: list[int | None], gamma: float = 0.95,
                       terminal_penalty: float | None = None) -> list[float | None]:
    """折扣回报 G_t；窗口内任一分为 None 时该步返回 None（不伪造）。

    `terminal_penalty` 仅在最后一步分数为 None 且调用方明确给出时生效。
    """
    values = list(scores)
    if terminal_penalty is not None and values and values[-1] is None:
        values[-1] = int(terminal_penalty)
    out: list[float | None] = []
    total_steps = len(values)
    for start in range(total_steps):
        window = values[start:]
        if any(v is None for v in window):
            out.append(None)
            continue
        total = sum((gamma ** (offset)) * float(value)
                    for offset, value in enumerate(window))
        out.append(round(total, 6))
    return out


def extract_steps(traces: list[dict[str, Any]], goal: str,
                  workspace: str | None = None) -> list[dict[str, Any]]:
    """traces → step 列表（每个工具调用一步）。

    输入 trace 行：{seq, patch_data}；patch_data 可为 dict 或 JSON 字符串。
    只消费 `_executed_tool_calls` 与 `tool_results` 摘要；完整结果不在 trace。
    """
    steps: list[dict[str, Any]] = []
    for row in traces:
        patch = row.get("patch_data")
        if isinstance(patch, str):
            try:
                patch = json.loads(patch)
            except ValueError:
                continue
        if not isinstance(patch, dict):
            continue
        calls = patch.get("_executed_tool_calls") or []
        if not isinstance(calls, list):
            continue
        result_digest = patch.get("tool_results") or {}
        items = result_digest.get("items") if isinstance(result_digest, dict) else None
        items = items if isinstance(items, list) else []
        seq = row.get("seq")
        iteration = patch.get("iteration") if isinstance(patch.get("iteration"), int) else seq
        for index, call in enumerate(calls):
            if not isinstance(call, dict):
                continue
            action = normalize_action(
                raw=call.get("arguments") or call.get("raw") or "",
                tool=call.get("name"),
                args=call.get("args") if isinstance(call.get("args"), dict) else None,
                workspace=workspace,
            )
            item = items[index] if index < len(items) and isinstance(items[index], dict) else {}
            observation = {
                "digest": (f"{item.get('tool_name') or action['tool']}:"
                           f"{item.get('chars', 0)}chars") if item else "digest:unavailable",
                "error": None,
                "tool_result_ref": f"trace:{seq}:{index}",
            }
            step_index = len(steps)
            steps.append({
                "step": step_index,
                "trace_seq": seq,
                "call_index": index,
                "iteration": iteration,
                "state": make_state(goal, step_index, observation["digest"], seq),
                "action": action,
                "observation": observation,
                "credit": {
                    "review_score": None,
                    "source": "review",
                    "review_id": None,
                    "dimension": None,
                    "confidence": None,
                    "evidence_ref": None,
                    "root_cause": None,
                    "discounted_return": None,
                },
            })
    return steps


def _credit_index(step_credits: list[dict[str, Any]]) -> tuple[dict[str, dict], dict[int, dict]]:
    by_ref: dict[str, dict] = {}
    by_iter: dict[int, dict] = {}
    for item in step_credits:
        if not isinstance(item, dict):
            continue
        ref = item.get("step_ref")
        if isinstance(ref, str) and ref:
            by_ref[ref] = item
        iteration = item.get("iteration")
        if isinstance(iteration, int) and iteration not in by_iter:
            by_iter[iteration] = item
    return by_ref, by_iter


def apply_credits(steps: list[dict[str, Any]], step_credits: list[dict[str, Any]],
                  review_id: str | None, gamma: float = 0.95) -> None:
    """把复盘 step_credits 对齐到 step；无匹配保持 None。"""
    by_ref, by_iter = _credit_index(step_credits)
    positional = len(step_credits) == len(steps)
    for step in steps:
        ref = step.get("observation", {}).get("tool_result_ref")
        candidate = by_ref.get(ref)
        if candidate is None:
            candidate = by_iter.get(step.get("iteration"))
        if candidate is None and positional:
            candidate = step_credits[step["step"]]
        credit = step["credit"]
        credit["review_id"] = review_id
        if candidate is None:
            continue
        score = candidate.get("score")
        if isinstance(score, bool) or not isinstance(score, int):
            score = None
        if score is not None and not (REVIEW_SCORE_MIN <= score <= REVIEW_SCORE_MAX):
            score = None
        credit["review_score"] = score
        credit["dimension"] = candidate.get("dimension")
        credit["confidence"] = candidate.get("confidence")
        credit["evidence_ref"] = candidate.get("evidence") or candidate.get("evidence_ref")
        credit["root_cause"] = candidate.get("root_cause")

    returns = discounted_returns([s["credit"]["review_score"] for s in steps], gamma)
    for step, value in zip(steps, returns, strict=True):
        step["credit"]["discounted_return"] = value


def build_episode(task: dict[str, Any], traces: list[dict[str, Any]],
                  review: dict[str, Any] | None, *, suite: str = "",
                  split: str = "train", gamma: float = 0.95,
                  episode_id: str | None = None) -> dict[str, Any] | None:
    """task + traces + review → `agentos.experience.v1` episode；无 step 返回 None。"""
    goal = str(task.get("goal") or task.get("description") or "")[:MAX_GOAL_CHARS]
    steps = extract_steps(traces, goal, workspace=task.get("ws_path"))
    if not steps:
        return None

    review = review or {}
    review_id = review.get("review_id")
    apply_credits(steps, review.get("step_credits") or [], review_id, gamma=gamma)

    status = str(task.get("status") or "")
    success = status in SUCCESS_STATUSES
    pipeline_id = str(task.get("pipeline_id") or "")
    episode_id = episode_id or f"exp_{pipeline_id}_{review_id or 'noreview'}"

    return {
        "schema_version": "agentos.experience.v1",
        "episode_id": episode_id,
        "source": {
            "kind": "task_run",
            "task_id": pipeline_id,
            "pipeline_id": pipeline_id,
            "agent_config_id": task.get("agent_id") or "",
            "model": task.get("model") or "",
            "suite_id": suite,
            "case_id": task.get("case_id"),
            "split": split,
        },
        "task": {
            "goal": goal,
            "acceptance_criteria": task.get("acceptance_criteria") or {},
        },
        "outcome": {
            "success": success,
            "score": task.get("score"),
            "task_status": status,
            "eval_ids": task.get("eval_ids") or [],
            "terminal_reward": 1.0 if success else 0.0,
        },
        "review": {
            "review_id": review_id,
            "report_ref": review.get("report_ref"),
            "status": review.get("status"),
            "overall_status": review.get("overall_status"),
            "credit_policy": review.get("credit_policy") or "review_credit_v1",
            "lessons_count": len(review.get("lessons") or []),
        },
        "steps": steps,
        "returns": {
            "gamma": gamma,
            "G": [step["credit"]["discounted_return"] for step in steps],
        },
        "provenance": {
            "trace_seqs": [step.get("trace_seq") for step in steps],
            "created_at": task.get("ended_at") or "",
            "code_version": "",
            "suite_version": task.get("suite_version") or "3",
            "license": "internal",
        },
    }


def validate_episode(episode: dict[str, Any]) -> list[str]:
    """结构校验（stdlib；JSON Schema 供 CI/外部工具使用）。"""
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(isinstance(episode, dict), "episode 不是对象")
    if not isinstance(episode, dict):
        return errors
    require(episode.get("schema_version") == "agentos.experience.v1",
            "schema_version 必须是 agentos.experience.v1")
    require(bool(episode.get("episode_id")), "episode_id 缺失")
    for key in ("source", "task", "outcome", "review", "returns", "provenance"):
        require(isinstance(episode.get(key), dict), f"{key} 缺失或不是对象")
    source = episode.get("source") or {}
    require(bool(source.get("task_id")), "source.task_id 缺失")
    require(bool(source.get("pipeline_id")), "source.pipeline_id 缺失")
    require(bool(source.get("suite_id")), "source.suite_id 缺失")
    require(source.get("split") in {"train", "heldout", "ood"},
            "source.split 必须是 train/heldout/ood")

    steps = episode.get("steps")
    require(isinstance(steps, list) and bool(steps), "steps 必须是非空数组")
    returns = episode.get("returns") or {}
    g_values = returns.get("G")
    if isinstance(steps, list):
        require(isinstance(g_values, list) and len(g_values) == len(steps),
                "returns.G 长度必须等于 steps")
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if not isinstance(step, dict):
            errors.append(f"steps[{index}] 不是对象")
            continue
        state = step.get("state") or {}
        action = step.get("action") or {}
        credit = step.get("credit") or {}
        require(isinstance(state.get("repr"), str) and bool(state.get("repr")),
                f"steps[{index}].state.repr 缺失")
        require(isinstance(state.get("digest"), str) and bool(state.get("digest")),
                f"steps[{index}].state.digest 缺失")
        require(isinstance(action.get("normalized"), str) and bool(action.get("normalized")),
                f"steps[{index}].action.normalized 缺失")
        require(isinstance(action.get("tool"), str) and bool(action.get("tool")),
                f"steps[{index}].action.tool 缺失")
        score = credit.get("review_score")
        if score is not None:
            require(isinstance(score, int) and not isinstance(score, bool)
                    and REVIEW_SCORE_MIN <= score <= REVIEW_SCORE_MAX,
                    f"steps[{index}].credit.review_score 必须是 {REVIEW_SCORE_MIN}..{REVIEW_SCORE_MAX} 或 null")
        require("discounted_return" in credit, f"steps[{index}].credit.discounted_return 缺失")
    return errors


def quality_gates(episodes: list[dict[str, Any]],
                  heldout_case_ids: set[str] | None = None,
                  min_review_coverage: float = 0.8,
                  max_dup_ratio: float = 0.1,
                  min_calibration: float = 0.7) -> dict[str, Any]:
    """质量闸：schema / 复盘覆盖 / 去重 / 泄漏 / 校准。"""
    schema_errors = {str(ep.get("episode_id") or i): validate_episode(ep)
                     for i, ep in enumerate(episodes)}
    schema_valid = not any(schema_errors.values())

    total = len(episodes)
    reviewed = sum(1 for ep in episodes
                   if (ep.get("review") or {}).get("review_id")
                   and (ep.get("review") or {}).get("status") == "completed"
                   and (ep.get("review") or {}).get("report_ref"))
    review_coverage = round(reviewed / total, 4) if total else 0.0

    digests: list[str] = []
    for episode in episodes:
        for step in episode.get("steps") or []:
            digest = (step.get("state") or {}).get("digest")
            if digest:
                digests.append(str(digest))
    duplicates = len(digests) - len(set(digests))
    dup_ratio = round(duplicates / len(digests), 4) if digests else 1.0

    leakage: int | None = None
    if heldout_case_ids is not None:
        train_cases = {str((ep.get("source") or {}).get("case_id"))
                       for ep in episodes
                       if (ep.get("source") or {}).get("split") == "train"
                       and (ep.get("source") or {}).get("case_id")}
        leakage = len(train_cases & heldout_case_ids)

    success_scores: list[float] = []
    failure_scores: list[float] = []
    for episode in episodes:
        scores = [s.get("credit", {}).get("review_score") for s in episode.get("steps") or []]
        numeric = [float(v) for v in scores if isinstance(v, int) and not isinstance(v, bool)]
        if not numeric:
            continue
        mean_score = sum(numeric) / len(numeric)
        if (episode.get("outcome") or {}).get("success"):
            success_scores.append(mean_score)
        else:
            failure_scores.append(mean_score)
    calibration: float | None = None
    if success_scores and failure_scores:
        hits = sum(1 for v in success_scores if v > 0) + sum(1 for v in failure_scores if v < 0)
        calibration = round(hits / (len(success_scores) + len(failure_scores)), 4)

    gates = {
        "schema_valid": schema_valid,
        "review_coverage": review_coverage,
        "review_coverage_ok": review_coverage >= min_review_coverage,
        "dup_ratio": dup_ratio,
        "dedup_ok": dup_ratio < max_dup_ratio,
        "leakage": leakage,
        "leakage_checked": heldout_case_ids is not None,
        "leakage_ok": leakage in (None, 0),
        "calibration": calibration,
        "calibration_ok": calibration is not None and calibration >= min_calibration,
        "episodes": total,
        "steps": len(digests),
    }
    gates["all_passed"] = bool(
        gates["schema_valid"]
        and gates["review_coverage_ok"]
        and gates["dedup_ok"]
        and gates["leakage_ok"]
        and gates["calibration_ok"]
    )
    if not schema_valid:
        gates["schema_errors"] = {k: v for k, v in schema_errors.items() if v}
    return gates


def _auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return round(wins / (len(pos) * len(neg)), 4)


def leave_one_out_auc(episodes: list[dict[str, Any]], k: int = 8) -> dict[str, Any]:
    """离线留一验证：邻居回报能否预测当前动作好/坏。

    对每个有复盘分的 step，检索其它 episode 的 top-k 相似状态；
    预测信号 = 同动作邻居平均回报 − 全体邻居平均回报；标签 = 复盘分 > 0。
    """
    records: list[dict[str, Any]] = []
    for episode in episodes:
        for step in episode.get("steps") or []:
            credit = step.get("credit") or {}
            score = credit.get("review_score")
            value = credit.get("discounted_return")
            if not isinstance(score, int) or isinstance(score, bool):
                continue
            if value is None or score == 0:
                continue
            records.append({
                "episode_id": episode.get("episode_id"),
                "step": step.get("step"),
                "tokens": tokenize((step.get("state") or {}).get("repr", "")),
                "action": (step.get("action") or {}).get("normalized", ""),
                "value": float(value),
                "label": 1 if score > 0 else 0,
            })

    positives: list[float] = []
    negatives: list[float] = []
    for index, query in enumerate(records):
        scored: list[tuple[float, dict[str, Any]]] = []
        for other_index, other in enumerate(records):
            if other_index == index:
                continue
            similarity = jaccard(query["tokens"], other["tokens"])
            if similarity > 0:
                scored.append((similarity, other))
        scored.sort(key=lambda item: item[0], reverse=True)
        neighbors = [item[1] for item in scored[:k]]
        if not neighbors:
            continue
        baseline = sum(n["value"] for n in neighbors) / len(neighbors)
        same = [n for n in neighbors if n["action"] == query["action"]]
        action_value = sum(n["value"] for n in same) / len(same) if same else baseline
        signal = action_value - baseline
        if query["label"]:
            positives.append(signal)
        else:
            negatives.append(signal)

    return {
        "auc": _auc(positives, negatives),
        "positives": len(positives),
        "negatives": len(negatives),
        "queries": len(records),
        "k": k,
    }
