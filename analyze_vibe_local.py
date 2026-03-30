#!/usr/bin/env python3
"""
vibe-local セッション JSONL から telemetry state / summary を抽出して
Claude 用 analyze_session.py と互換の JSON 契約で出力する。
"""

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from analyze_session import print_text_report
from parse_session import classify_tool, parse_timestamp

_MISSING = object()
_CONTRACT_SCORE_THRESHOLD = 5
_CONTRACT_FIELD_ALIASES = {
    "sessionId": ("sessionId", "session_id"),
    "sessionPath": ("sessionPath", "session_path"),
    "period": ("period",),
    "eventCount": ("eventCount", "event_count"),
    "turnCount": ("turnCount", "turn_count"),
    "totalToolCalls": ("totalToolCalls", "total_tool_calls"),
    "totalErrors": ("totalErrors", "total_errors"),
    "errorRate": ("errorRate", "error_rate"),
    "tokenUsage": ("tokenUsage", "token_usage"),
    "toolStats": ("toolStats", "tool_stats"),
    "categoryBreakdown": ("categoryBreakdown", "category_breakdown"),
    "hourlyActivity": ("hourlyActivity", "hourly_activity"),
    "slowestTurns": ("slowestTurns", "slowest_turns"),
    "waitTime": ("waitTime", "wait_time"),
    "sessionMetadata": ("sessionMetadata", "session_metadata"),
}
_SESSION_METADATA_KEYS = {
    "assistantModels": ("assistantModels", "assistant_models"),
    "versions": ("versions",),
    "gitBranches": ("gitBranches", "git_branches"),
    "entrypoints": ("entrypoints",),
    "agentNames": ("agentNames", "agent_names"),
    "recordTypes": ("recordTypes", "record_types"),
}


class VibeLocalAnalysisError(ValueError):
    """Base error for vibe-local analysis failures."""


class TelemetrySummaryNotFoundError(VibeLocalAnalysisError):
    """Raised when no telemetry summary payload can be found."""


class TelemetrySummaryFormatError(VibeLocalAnalysisError):
    """Raised when a telemetry summary exists but is malformed."""


def _round(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _alias_names(name: str) -> set[str]:
    normalized = name.replace("-", "_")
    aliases = {name, normalized, normalized.replace("_", "-")}
    if "_" in normalized:
        parts = normalized.split("_")
        aliases.add(parts[0] + "".join(part.capitalize() for part in parts[1:]))
    else:
        aliases.add(re.sub(r"(?<!^)(?=[A-Z])", "_", normalized).lower())
    return {alias for alias in aliases if alias}


def _lookup(mapping: dict, *names: str, default=_MISSING):
    for name in names:
        for alias in _alias_names(name):
            if alias in mapping and mapping[alias] is not None:
                return mapping[alias]
    return default


def _coerce_float(value, default=None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip().rstrip("%")
        if not stripped:
            return default
        try:
            return float(stripped)
        except ValueError:
            return default
    return default


def _coerce_int(value, default=0) -> int | None:
    coerced = _coerce_float(value, default=None)
    if coerced is None:
        return default
    return int(coerced)


def _coerce_datetime(value, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = value / 1000 if value > 1_000_000_000_000 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return parse_timestamp(value)
        except ValueError as exc:
            raise TelemetrySummaryFormatError(
                f"Invalid {field_name} timestamp: {value!r}"
            ) from exc
    raise TelemetrySummaryFormatError(f"Unsupported {field_name} timestamp: {value!r}")


def _coerce_timestamp(value, field_name: str) -> str:
    return _coerce_datetime(value, field_name).isoformat()


def _normalize_string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        candidates = [value]
    cleaned = {str(item).strip() for item in candidates if str(item).strip()}
    return sorted(cleaned)


def _load_records(path: Path) -> list[tuple[int, dict]]:
    records: list[tuple[int, dict]] = []
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"  skip line {line_no}: invalid JSON", file=sys.stderr)
                continue
            if not isinstance(record, dict):
                print(f"  skip line {line_no}: JSON value is not an object", file=sys.stderr)
                continue
            records.append((line_no, record))
    return records


def _iter_candidate_dicts(value, path: tuple = ()):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from _iter_candidate_dicts(child, path + (key,))
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_candidate_dicts(child, path + (index,))


def _is_message_record_type(record_type: str) -> bool:
    lowered = record_type.strip().lower()
    if not lowered:
        return False
    if any(token in lowered for token in ("telemetry", "summary", "analysis", "metric")):
        return False
    return (
        lowered in {"message", "user", "assistant", "system", "chat"}
        or lowered.endswith("_message")
        or lowered.endswith("message")
    )


def _record_priority(record: dict) -> int:
    record_type = str(record.get("record_type") or "").strip().lower()
    if not record_type:
        return 0
    if _is_message_record_type(record_type):
        return -5
    priority = 1
    if any(token in record_type for token in ("telemetry", "summary", "analysis", "metric")):
        priority += 5
    return priority


def _matched_contract_fields(candidate: dict) -> set[str]:
    matched_fields = set()
    for field_name, aliases in _CONTRACT_FIELD_ALIASES.items():
        if _lookup(candidate, *aliases, default=_MISSING) is not _MISSING:
            matched_fields.add(field_name)
    return matched_fields


def _score_candidate(candidate: dict) -> tuple[int, set[str]]:
    matched_fields = _matched_contract_fields(candidate)
    score = len(matched_fields)
    for field_name in {"period", "tokenUsage", "toolStats", "sessionMetadata"} & matched_fields:
        score += 2
    return score, matched_fields


def _select_best_candidate(records: list[tuple[int, dict]]):
    best = None
    for line_no, record in records:
        record_priority = _record_priority(record)
        record_type = record.get("record_type")
        for payload_path, candidate in _iter_candidate_dicts(record):
            raw_score, matched_fields = _score_candidate(candidate)
            score = raw_score + record_priority
            if best is None or score > best["score"] or (
                score == best["score"] and line_no >= best["lineNo"]
            ):
                best = {
                    "score": score,
                    "rawScore": raw_score,
                    "matchedFields": matched_fields,
                    "lineNo": line_no,
                    "recordType": record_type,
                    "payloadPath": payload_path,
                    "payload": candidate,
                }
    return best


def _is_summary_candidate(candidate: dict | None) -> bool:
    if candidate is None:
        return False
    matched_fields = candidate.get("matchedFields", set())
    return (
        candidate.get("rawScore", 0) >= _CONTRACT_SCORE_THRESHOLD
        and len(matched_fields) >= 4
        and "period" in matched_fields
        and ("toolStats" in matched_fields or "tokenUsage" in matched_fields)
    )


def _find_telemetry_summary(records: list[tuple[int, dict]]) -> dict:
    telemetry_records = [
        (line_no, record)
        for line_no, record in records
        if record.get("record_type")
        and not _is_message_record_type(str(record.get("record_type")))
    ]
    best = _select_best_candidate(telemetry_records) if telemetry_records else None
    if not _is_summary_candidate(best):
        best = _select_best_candidate(records)
    if not _is_summary_candidate(best):
        raise TelemetrySummaryNotFoundError(
            "No normalized telemetry summary record was found. "
            "analyze_vibe_local.py expects an instrumented vibe-local session JSONL "
            "containing a non-message telemetry record with a summary payload."
        )
    return best


def _collect_record_timestamps(records: list[tuple[int, dict]]) -> list[datetime]:
    timestamps = []
    for _, record in records:
        for key in ("timestamp", "createdAt", "created_at", "updatedAt", "updated_at"):
            value = _lookup(record, key, default=_MISSING)
            if value is _MISSING:
                continue
            try:
                timestamps.append(_coerce_datetime(value, key))
                break
            except TelemetrySummaryFormatError:
                continue
    return timestamps


def _normalize_period(payload: dict, records: list[tuple[int, dict]]) -> dict:
    period_raw = _lookup(payload, "period", default={})
    if not isinstance(period_raw, dict):
        period_raw = {}

    timestamps = _collect_record_timestamps(records)
    fallback_start = min(timestamps) if timestamps else None
    fallback_end = max(timestamps) if timestamps else None

    start_value = _lookup(period_raw, "start", "startTime", "startedAt", default=_MISSING)
    if start_value is _MISSING:
        start_value = _lookup(payload, "start", "startTime", "startedAt", default=_MISSING)
    if start_value is _MISSING and fallback_start is not None:
        start_value = fallback_start

    end_value = _lookup(period_raw, "end", "endTime", "endedAt", default=_MISSING)
    if end_value is _MISSING:
        end_value = _lookup(payload, "end", "endTime", "endedAt", default=_MISSING)
    if end_value is _MISSING and fallback_end is not None:
        end_value = fallback_end

    if start_value is _MISSING or end_value is _MISSING:
        raise TelemetrySummaryFormatError(
            "Telemetry summary is missing period start/end timestamps."
        )

    start = _coerce_timestamp(start_value, "period.start")
    end = _coerce_timestamp(end_value, "period.end")
    duration_seconds = _coerce_float(
        _lookup(period_raw, "durationSeconds", "duration", default=_MISSING),
        default=None,
    )
    if duration_seconds is None:
        duration_seconds = _coerce_float(
            _lookup(payload, "durationSeconds", "duration", default=_MISSING),
            default=None,
        )
    if duration_seconds is None:
        duration_seconds = (parse_timestamp(end) - parse_timestamp(start)).total_seconds()

    return {
        "start": start,
        "end": end,
        "durationSeconds": _round(duration_seconds),
    }


def _collect_record_metadata(records: list[tuple[int, dict]]) -> dict:
    metadata = {
        "assistantModels": set(),
        "versions": set(),
        "gitBranches": set(),
        "entrypoints": set(),
        "agentNames": set(),
        "recordTypes": set(),
    }
    for _, record in records:
        if record.get("record_type"):
            metadata["recordTypes"].add(str(record["record_type"]))

        if value := record.get("version"):
            metadata["versions"].add(str(value))
        if value := record.get("gitBranch"):
            metadata["gitBranches"].add(str(value))
        if value := record.get("entrypoint"):
            metadata["entrypoints"].add(str(value))
        if value := record.get("agentName"):
            metadata["agentNames"].add(str(value))
        if value := record.get("model"):
            metadata["assistantModels"].add(str(value))

        message = record.get("message")
        if isinstance(message, dict):
            if value := message.get("model"):
                metadata["assistantModels"].add(str(value))
            if value := message.get("agentName"):
                metadata["agentNames"].add(str(value))

    return {key: sorted(values) for key, values in metadata.items()}


def _normalize_metadata(
    payload: dict,
    records: list[tuple[int, dict]],
    record_type: str | None,
) -> dict:
    raw_metadata = _lookup(payload, "sessionMetadata", "session_metadata", default={})
    if not isinstance(raw_metadata, dict):
        raw_metadata = {}

    metadata = dict(raw_metadata)
    record_metadata = _collect_record_metadata(records)
    for field_name, aliases in _SESSION_METADATA_KEYS.items():
        value = _lookup(raw_metadata, *aliases, default=_MISSING)
        combined_values = []
        if value is not _MISSING:
            combined_values.extend(_normalize_string_list(value))
        combined_values.extend(record_metadata.get(field_name, []))
        metadata[field_name] = _normalize_string_list(combined_values)

    metadata["source"] = str(metadata.get("source") or "vibe-local")
    if record_type and not metadata.get("telemetryRecordType"):
        metadata["telemetryRecordType"] = str(record_type)
    return metadata


def _normalize_token_usage(payload: dict) -> dict:
    raw_usage = _lookup(payload, "tokenUsage", "token_usage", default={})
    if not isinstance(raw_usage, dict):
        raw_usage = {}

    input_tokens = _coerce_int(
        _lookup(raw_usage, "input", "inputTokens", "input_tokens", default=_MISSING),
        default=0,
    )
    if input_tokens == 0:
        input_tokens = _coerce_int(
            _lookup(payload, "inputTokens", "input_tokens", default=0),
            default=0,
        )
    output_tokens = _coerce_int(
        _lookup(raw_usage, "output", "outputTokens", "output_tokens", default=_MISSING),
        default=0,
    )
    if output_tokens == 0:
        output_tokens = _coerce_int(
            _lookup(payload, "outputTokens", "output_tokens", default=0),
            default=0,
        )
    cache_read = _coerce_int(
        _lookup(raw_usage, "cacheRead", "cacheReadTokens", "cache_read_tokens", default=_MISSING),
        default=0,
    )
    if cache_read == 0:
        cache_read = _coerce_int(
            _lookup(payload, "cacheReadTokens", "cache_read_tokens", default=0),
            default=0,
        )
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read,
        "total": input_tokens + output_tokens,
    }


def _normalize_stat_items(raw_value, name_key: str) -> list[dict]:
    if isinstance(raw_value, list):
        return [item for item in raw_value if isinstance(item, dict)]
    if isinstance(raw_value, dict):
        items = []
        for name, value in raw_value.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault(name_key, name)
            else:
                item = {name_key: name, "count": value}
            items.append(item)
        return items
    return []


def _normalize_tool_stats(raw_value, total_tool_calls: int) -> list[dict]:
    tool_stats = []
    for item in _normalize_stat_items(raw_value, "name"):
        name = str(_lookup(item, "name", "tool", "toolName", default="unknown"))
        count = _coerce_int(_lookup(item, "count", "calls", default=0), default=0)
        error_count = _coerce_int(
            _lookup(item, "errorCount", "error_count", "errors", default=0),
            default=0,
        )
        avg_duration = _coerce_float(
            _lookup(
                item,
                "avgDurationSeconds",
                "avg_duration_seconds",
                "averageDurationSeconds",
                "averageSeconds",
                default=0,
            ),
            default=0.0,
        )
        total_duration = _coerce_float(
            _lookup(
                item,
                "totalDurationSeconds",
                "total_duration_seconds",
                "durationSeconds",
                "duration_seconds",
                default=_MISSING,
            ),
            default=None,
        )
        if total_duration is None:
            total_duration = avg_duration * count

        percentage = _coerce_float(
            _lookup(item, "percentage", "percent", "share", default=_MISSING),
            default=None,
        )
        if percentage is None:
            percentage = _round((count / total_tool_calls) * 100, 1) if total_tool_calls else 0.0

        tool_stats.append({
            "name": name,
            "category": str(_lookup(item, "category", default=classify_tool(name))),
            "count": count,
            "percentage": _round(percentage, 1),
            "avgDurationSeconds": _round(avg_duration),
            "totalDurationSeconds": _round(total_duration),
            "errorCount": error_count,
        })

    tool_stats.sort(key=lambda item: (-item["count"], item["name"]))
    return tool_stats


def _derive_category_breakdown(tool_stats: list[dict]) -> list[dict]:
    counts = Counter()
    durations = defaultdict(float)
    for stat in tool_stats:
        counts[stat["category"]] += stat["count"]
        durations[stat["category"]] += stat["totalDurationSeconds"]
    return [
        {
            "category": category,
            "count": count,
            "totalDurationSeconds": _round(durations[category]),
        }
        for category, count in counts.most_common()
    ]


def _normalize_category_breakdown(raw_value, tool_stats: list[dict]) -> list[dict]:
    items = []
    for item in _normalize_stat_items(raw_value, "category"):
        category = str(_lookup(item, "category", "name", default="other"))
        count = _coerce_int(_lookup(item, "count", "calls", default=0), default=0)
        total_duration = _coerce_float(
            _lookup(
                item,
                "totalDurationSeconds",
                "total_duration_seconds",
                "durationSeconds",
                "duration_seconds",
                default=0,
            ),
            default=0.0,
        )
        items.append({
            "category": category,
            "count": count,
            "totalDurationSeconds": _round(total_duration),
        })
    if items:
        items.sort(key=lambda item: (-item["count"], item["category"]))
        return items
    return _derive_category_breakdown(tool_stats)


def _normalize_hourly_activity(raw_value) -> list[dict]:
    counts = {hour: 0 for hour in range(24)}
    if isinstance(raw_value, dict):
        iterable = [{"hour": hour, "count": count} for hour, count in raw_value.items()]
    elif isinstance(raw_value, list):
        iterable = raw_value
    else:
        iterable = []

    for item in iterable:
        if not isinstance(item, dict):
            continue
        hour = _coerce_int(_lookup(item, "hour", default=_MISSING), default=None)
        if hour is None or not 0 <= hour <= 23:
            continue
        counts[hour] = _coerce_int(_lookup(item, "count", "toolCalls", "value", default=0), default=0)
    return [{"hour": hour, "count": counts[hour]} for hour in range(24)]


def _normalize_slowest_turns(raw_value) -> list[dict]:
    if not isinstance(raw_value, list):
        return []

    turns = []
    for item in raw_value:
        if not isinstance(item, dict):
            continue
        tools = _lookup(item, "tools", "toolNames", default=[])
        if isinstance(tools, str):
            tools = [tools]
        elif not isinstance(tools, list):
            tools = []

        turns.append({
            "turnIndex": _coerce_int(
                _lookup(item, "turnIndex", "turn_index", "index", default=0),
                default=0,
            ),
            "durationSeconds": _round(
                _coerce_float(
                    _lookup(
                        item,
                        "durationSeconds",
                        "duration_seconds",
                        "duration",
                        default=0,
                    ),
                    default=0.0,
                )
            ),
            "tools": [str(tool) for tool in tools if str(tool).strip()],
        })

    turns.sort(key=lambda item: item["durationSeconds"], reverse=True)
    return turns[:10]


def _normalize_wait_time(payload: dict) -> dict:
    raw_wait = _lookup(payload, "waitTime", "wait_time", default={})
    if not isinstance(raw_wait, dict):
        raw_wait = {}
    return {
        "averageSeconds": _coerce_float(
            _lookup(raw_wait, "averageSeconds", "average_seconds", "avg", default=None),
            default=None,
        ),
        "medianSeconds": _coerce_float(
            _lookup(raw_wait, "medianSeconds", "median_seconds", default=None),
            default=None,
        ),
        "maxSeconds": _coerce_float(
            _lookup(raw_wait, "maxSeconds", "max_seconds", default=None),
            default=None,
        ),
        "totalSeconds": _coerce_float(
            _lookup(raw_wait, "totalSeconds", "total_seconds", default=None),
            default=None,
        ),
    }


def _load_telemetry_state(records: list[tuple[int, dict]]) -> dict | None:
    for _, record in reversed(records):
        if record.get("record_type") == "vibe_local_telemetry":
            state = record.get("state")
            if isinstance(state, dict):
                return state
    return None


def _normalize_state_turns(state: dict) -> list[dict]:
    turns = []
    for raw_turn in state.get("turns") or []:
        if not isinstance(raw_turn, dict):
            continue
        start_value = _lookup(raw_turn, "start", default=_MISSING)
        end_value = _lookup(raw_turn, "end", default=_MISSING)
        if start_value is _MISSING or end_value is _MISSING:
            continue
        start = _coerce_timestamp(start_value, "turn.start")
        end = _coerce_timestamp(end_value, "turn.end")
        tools = []
        for raw_tool in raw_turn.get("tools") or []:
            if not isinstance(raw_tool, dict):
                continue
            tool_start_value = _lookup(raw_tool, "start", default=start)
            tool_end_value = _lookup(raw_tool, "end", default=tool_start_value)
            tool_name = str(_lookup(raw_tool, "name", "toolName", default="unknown"))
            tools.append({
                "id": str(_lookup(raw_tool, "id", default="")),
                "name": tool_name,
                "category": str(_lookup(raw_tool, "category", default=classify_tool(tool_name))),
                "start": _coerce_timestamp(tool_start_value, f"tool[{tool_name}].start"),
                "end": _coerce_timestamp(tool_end_value, f"tool[{tool_name}].end"),
                "error": bool(_lookup(raw_tool, "error", "isError", default=False)),
            })
        turns.append({
            "turnIndex": _coerce_int(_lookup(raw_turn, "turnIndex", "turn_index", default=len(turns) + 1), default=len(turns) + 1),
            "start": start,
            "end": end,
            "inputTokens": _coerce_int(_lookup(raw_turn, "inputTokens", "input_tokens", default=0), default=0),
            "outputTokens": _coerce_int(_lookup(raw_turn, "outputTokens", "output_tokens", default=0), default=0),
            "cacheReadTokens": _coerce_int(_lookup(raw_turn, "cacheReadTokens", "cache_read_tokens", default=0), default=0),
            "tools": tools,
        })
    return turns


def _build_analysis_from_state(
    session_path: Path,
    state: dict,
    records: list[tuple[int, dict]],
) -> dict:
    turns = _normalize_state_turns(state)
    if not turns:
        raise TelemetrySummaryFormatError(
            "vibe_local_telemetry record exists but does not contain any turn telemetry."
        )

    tool_counts = Counter()
    tool_durations: dict[str, list[float]] = defaultdict(list)
    tool_errors: Counter[str] = Counter()
    tool_categories: dict[str, str] = {}
    category_counts = Counter()
    category_durations: dict[str, float] = defaultdict(float)
    hour_counts = Counter()
    slowest_turns = []
    wait_times = []
    total_input = 0
    total_output = 0
    total_cache = 0

    for index, turn in enumerate(turns):
        turn_tools = []
        total_input += turn["inputTokens"]
        total_output += turn["outputTokens"]
        total_cache += turn["cacheReadTokens"]

        turn_start = parse_timestamp(turn["start"])
        turn_end = parse_timestamp(turn["end"])
        turn_duration = max((turn_end - turn_start).total_seconds(), 0.0)
        slowest_turns.append({
            "turnIndex": int(turn["turnIndex"] or index + 1),
            "durationSeconds": _round(turn_duration),
            "tools": [],
        })

        if index > 0:
            prev_end = parse_timestamp(turns[index - 1]["end"])
            wait = (turn_start - prev_end).total_seconds()
            if 0 < wait < 3600:
                wait_times.append(wait)

        for tool in turn["tools"]:
            name = tool["name"]
            category = tool["category"] or classify_tool(name)
            tool_counts[name] += 1
            tool_categories.setdefault(name, category)
            category_counts[category] += 1
            turn_tools.append(name)

            tool_start = parse_timestamp(tool["start"])
            tool_end = parse_timestamp(tool["end"])
            duration = max((tool_end - tool_start).total_seconds(), 0.0)
            tool_durations[name].append(duration)
            category_durations[category] += duration
            hour_counts[tool_start.hour] += 1
            if tool["error"]:
                tool_errors[name] += 1

        slowest_turns[-1]["tools"] = turn_tools

    total_tool_calls = sum(tool_counts.values())
    tool_stats = []
    for name, count in tool_counts.most_common():
        durations = tool_durations.get(name, [])
        total_duration = sum(durations)
        avg_duration = total_duration / len(durations) if durations else 0.0
        tool_stats.append({
            "name": name,
            "category": tool_categories.get(name, classify_tool(name)),
            "count": count,
            "percentage": _round((count / total_tool_calls) * 100, 1) if total_tool_calls else 0.0,
            "avgDurationSeconds": _round(avg_duration),
            "totalDurationSeconds": _round(total_duration),
            "errorCount": tool_errors.get(name, 0),
        })

    category_breakdown = [
        {
            "category": category,
            "count": count,
            "totalDurationSeconds": _round(category_durations.get(category, 0.0)),
        }
        for category, count in category_counts.most_common()
    ]

    hourly_activity = [{"hour": hour, "count": hour_counts.get(hour, 0)} for hour in range(24)]
    slowest_turns = sorted(slowest_turns, key=lambda turn: turn["durationSeconds"], reverse=True)[:10]

    wait_summary = {
        "averageSeconds": _round(sum(wait_times) / len(wait_times), 1) if wait_times else None,
        "medianSeconds": _round(statistics.median(wait_times), 1) if wait_times else None,
        "maxSeconds": _round(max(wait_times), 1) if wait_times else None,
        "totalSeconds": _round(sum(wait_times), 1) if wait_times else None,
    }

    total_errors = sum(tool_errors.values())
    period_start = turns[0]["start"]
    period_end = turns[-1]["end"]
    created_at = _lookup(state, "createdAt", "created_at", default=_MISSING)
    updated_at = _lookup(state, "updatedAt", "updated_at", default=_MISSING)
    if created_at is not _MISSING:
        period_start = _coerce_timestamp(created_at, "createdAt")
    if updated_at is not _MISSING:
        period_end = _coerce_timestamp(updated_at, "updatedAt")

    metadata = _normalize_metadata(
        {"sessionMetadata": state.get("sessionMetadata") or {}},
        records,
        "vibe_local_telemetry",
    )

    return {
        "sessionId": str(_lookup(state, "sessionId", "session_id", default=session_path.stem)),
        "sessionPath": str(session_path),
        "period": {
            "start": period_start,
            "end": period_end,
            "durationSeconds": _round(
                max((parse_timestamp(period_end) - parse_timestamp(period_start)).total_seconds(), 0.0)
            ),
        },
        "eventCount": _coerce_int(
            _lookup(state, "messageCount", "message_count", default=len([record for _, record in records if record.get("role")])),
            default=len([record for _, record in records if record.get("role")]),
        ),
        "turnCount": len(turns),
        "totalToolCalls": total_tool_calls,
        "totalErrors": total_errors,
        "errorRate": _round((total_errors / total_tool_calls) * 100, 1) if total_tool_calls else 0.0,
        "tokenUsage": {
            "input": total_input,
            "output": total_output,
            "cacheRead": total_cache,
            "total": total_input + total_output,
        },
        "toolStats": tool_stats,
        "categoryBreakdown": category_breakdown,
        "hourlyActivity": hourly_activity,
        "slowestTurns": slowest_turns,
        "waitTime": wait_summary,
        "sessionMetadata": metadata,
    }


def _derive_turn_count(records: list[tuple[int, dict]]) -> int:
    turn_count = 0
    for _, record in records:
        role = str(record.get("role") or record.get("type") or record.get("record_type") or "").lower()
        if role == "user" or role.endswith("user_message"):
            turn_count += 1
    return turn_count


def build_analysis(session_path: Path) -> dict:
    records = _load_records(session_path)
    if not records:
        raise VibeLocalAnalysisError(f"No JSON object records found in {session_path}")

    telemetry_state = _load_telemetry_state(records)
    if telemetry_state is not None:
        return _build_analysis_from_state(session_path, telemetry_state, records)

    summary = _find_telemetry_summary(records)
    payload = summary["payload"]

    token_usage = _normalize_token_usage(payload)
    raw_tool_stats = _lookup(payload, "toolStats", "tool_stats", default=[])
    total_tool_calls = _coerce_int(
        _lookup(payload, "totalToolCalls", "total_tool_calls", default=None),
        default=None,
    )
    if total_tool_calls is None:
        total_tool_calls = sum(
            _coerce_int(_lookup(item, "count", "calls", default=0), default=0)
            for item in _normalize_stat_items(raw_tool_stats, "name")
        )
    tool_stats = _normalize_tool_stats(raw_tool_stats, total_tool_calls)

    total_errors = _coerce_int(
        _lookup(payload, "totalErrors", "total_errors", default=None),
        default=None,
    )
    if total_errors is None:
        total_errors = sum(stat["errorCount"] for stat in tool_stats)

    error_rate = _round((total_errors / total_tool_calls) * 100, 1) if total_tool_calls else 0.0

    return {
        "sessionId": str(_lookup(payload, "sessionId", "session_id", default=session_path.stem)),
        "sessionPath": str(session_path),
        "period": _normalize_period(payload, records),
        "eventCount": _coerce_int(
            _lookup(payload, "eventCount", "event_count", default=len(records)),
            default=len(records),
        ),
        "turnCount": _coerce_int(
            _lookup(payload, "turnCount", "turn_count", default=None),
            default=_derive_turn_count(records),
        ),
        "totalToolCalls": total_tool_calls,
        "totalErrors": total_errors,
        "errorRate": error_rate,
        "tokenUsage": token_usage,
        "toolStats": tool_stats,
        "categoryBreakdown": _normalize_category_breakdown(
            _lookup(payload, "categoryBreakdown", "category_breakdown", default=[]),
            tool_stats,
        ),
        "hourlyActivity": _normalize_hourly_activity(
            _lookup(payload, "hourlyActivity", "hourly_activity", default=[])
        ),
        "slowestTurns": _normalize_slowest_turns(
            _lookup(payload, "slowestTurns", "slowest_turns", default=[])
        ),
        "waitTime": _normalize_wait_time(payload),
        "sessionMetadata": _normalize_metadata(payload, records, summary["recordType"]),
    }


def analyze(session_path: Path, output_format: str = "text", pretty_json: bool = False) -> None:
    analysis = build_analysis(session_path)
    if output_format == "json":
        json.dump(
            analysis,
            sys.stdout,
            ensure_ascii=False,
            indent=2 if pretty_json else None,
        )
        if pretty_json:
            sys.stdout.write("\n")
        return

    print_text_report(analysis)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="vibe-local session log analysis"
    )
    parser.add_argument("session_file", nargs="?", type=Path, help="Path to .jsonl session file")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--pretty-json",
        action="store_true",
        help="Pretty-print JSON output",
    )
    args = parser.parse_args()

    if not args.session_file:
        parser.error("analyze_vibe_local.py requires an explicit session_file path")

    try:
        analyze(args.session_file, args.format, args.pretty_json)
    except VibeLocalAnalysisError as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
