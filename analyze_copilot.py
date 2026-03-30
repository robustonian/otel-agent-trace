#!/usr/bin/env python3
"""
GitHub Copilot CLI session telemetry analyzer.

Copilot CLI stores headless session data in `events.jsonl`. This analyzer emits the
same JSON contract as analyze_session.py so ts-bench can store Copilot telemetry in
the existing Supabase / dashboard schema.
"""

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from analyze_session import print_text_report
from parse_session import load_events, parse_timestamp


EXIT_CODE_PATTERN = re.compile(r"exit code (\d+)")


def _round(value: float, digits: int = 3) -> float:
    return round(value, digits)


def _as_record(value):
    return value if isinstance(value, dict) else {}


def _coerce_int(value, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _classify_tool(name: str) -> str:
    normalized = (name or "").lower()
    if normalized in {"view", "glob", "rg", "grep"}:
        return "file_read"
    if normalized in {"create", "edit", "apply_patch"}:
        return "file_write"
    if normalized in {"bash", "write_bash", "read_bash", "stop_bash", "list_bash"}:
        return "shell"
    if normalized in {"web_fetch"}:
        return "web"
    if normalized in {"task", "read_agent", "list_agents"}:
        return "agent"
    if normalized.startswith("github-mcp-server"):
        return "github"
    if normalized in {
        "report_intent",
        "fetch_copilot_cli_documentation",
        "sql",
        "ask_user",
        "exit_plan_mode",
    }:
        return "system"
    return "other"


def _result_text(data: dict) -> str:
    result = _as_record(data.get("result"))
    parts = [result.get("content"), result.get("detailedContent")]
    return "\n".join(part for part in parts if isinstance(part, str))


def _has_nonzero_exit_code(text: str) -> bool:
    return any(int(match) != 0 for match in EXIT_CODE_PATTERN.findall(text))


def _find_failed_subagent_ids(events: list[dict]) -> set[str]:
    return {
        str(_event_data(event).get("toolCallId") or "")
        for event in events
        if _event_type(event) == "subagent.failed" and _event_data(event).get("toolCallId")
    }


def _tool_is_error(name: str, data: dict, failed_subagent_ids: set[str]) -> bool:
    tool_call_id = str(data.get("toolCallId") or "")
    if not bool(data.get("success", True)):
        return True

    telemetry_properties = _as_record(_as_record(data.get("toolTelemetry")).get("properties"))
    status = str(telemetry_properties.get("status") or "").lower()
    if name == "task" and status in {"failed", "error", "cancelled"}:
        return True
    if tool_call_id in failed_subagent_ids:
        return True

    if name in {"bash", "read_bash", "write_bash", "stop_bash"} and _has_nonzero_exit_code(_result_text(data)):
        return True

    return False


def _event_data(event: dict) -> dict:
    return _as_record(event.get("data"))


def _event_type(event: dict) -> str:
    return str(event.get("type") or "")


def _find_exercise_task_call_id(events: list[dict], exercise: str) -> str | None:
    normalized_exercise = exercise.strip().lower()
    if not normalized_exercise:
        return None

    candidates: list[tuple[int, str, str]] = []
    for event in events:
        if _event_type(event) != "tool.execution_start":
            continue

        data = _event_data(event)
        if data.get("toolName") != "task":
            continue

        arguments = _as_record(data.get("arguments"))
        description = str(arguments.get("description") or "")
        prompt = str(arguments.get("prompt") or "")
        agent_type = str(arguments.get("agent_type") or "")
        agent_name = str(arguments.get("name") or "")
        text = " ".join([description, prompt, agent_type, agent_name]).lower()
        if normalized_exercise not in text:
            continue

        priority = 0
        if description.strip().lower() == f"solve {normalized_exercise}":
            priority += 10
        if agent_type.lower() == "exercise-worker" or agent_name.lower() == "exercise-worker":
            priority += 5
        if f"/{normalized_exercise}/" in prompt.lower() or f" {normalized_exercise} " in f" {text} ":
            priority += 1

        tool_call_id = str(data.get("toolCallId") or "")
        if tool_call_id:
            candidates.append((priority, str(event.get("timestamp") or ""), tool_call_id))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def _filter_events_for_task(events: list[dict], parent_tool_call_id: str) -> list[dict]:
    filtered = []
    for event in events:
        data = _event_data(event)
        event_type = _event_type(event)
        tool_call_id = str(data.get("toolCallId") or "")
        parent_id = str(data.get("parentToolCallId") or "")

        if parent_id == parent_tool_call_id:
            filtered.append(event)
            continue

        if tool_call_id == parent_tool_call_id and event_type in {
            "tool.execution_start",
            "tool.execution_complete",
            "subagent.started",
            "subagent.completed",
            "subagent.failed",
        }:
            filtered.append(event)

    return filtered


def _build_tool_records(events: list[dict], parent_tool_call_id: str | None = None) -> list[dict]:
    starts: dict[str, dict] = {}
    records: list[dict] = []
    failed_subagent_ids = _find_failed_subagent_ids(events)

    for event in events:
        event_type = _event_type(event)
        if event_type not in {"tool.execution_start", "tool.execution_complete"}:
            continue

        data = _event_data(event)
        tool_call_id = str(data.get("toolCallId") or "")
        parent_id = str(data.get("parentToolCallId") or "")

        if parent_tool_call_id is not None and tool_call_id != parent_tool_call_id and parent_id != parent_tool_call_id:
            continue

        if event_type == "tool.execution_start":
            starts[tool_call_id] = {
                "name": str(data.get("toolName") or "unknown"),
                "start": str(event.get("timestamp") or ""),
                "parentToolCallId": parent_id or None,
            }
            continue

        started = starts.get(tool_call_id)
        name = started["name"] if started else str(data.get("toolName") or "unknown")
        start_ts = started["start"] if started else str(event.get("timestamp") or "")
        records.append({
            "id": tool_call_id,
            "name": name,
            "start": start_ts,
            "end": str(event.get("timestamp") or ""),
            "parentToolCallId": parent_id or (started.get("parentToolCallId") if started else None),
            "error": _tool_is_error(name, data, failed_subagent_ids),
            "toolTelemetry": _as_record(data.get("toolTelemetry")),
        })

    return records


def _build_tool_stats(tool_records: list[dict]) -> tuple[list[dict], int, int]:
    tool_counts = Counter(record["name"] for record in tool_records)
    tool_durations: dict[str, list[float]] = defaultdict(list)
    tool_errors: Counter[str] = Counter()

    for record in tool_records:
        if record["start"] and record["end"]:
            duration = (parse_timestamp(record["end"]) - parse_timestamp(record["start"])).total_seconds()
            tool_durations[record["name"]].append(duration)
        if record["error"]:
            tool_errors[record["name"]] += 1

    total_tool_calls = len(tool_records)
    total_errors = sum(tool_errors.values())
    stats = []
    for name, count in tool_counts.most_common():
        durations = tool_durations.get(name, [])
        avg_duration = sum(durations) / len(durations) if durations else 0.0
        total_duration = sum(durations)
        stats.append({
            "name": name,
            "category": _classify_tool(name),
            "count": count,
            "percentage": _round((count / total_tool_calls) * 100, 1) if total_tool_calls else 0.0,
            "avgDurationSeconds": _round(avg_duration),
            "totalDurationSeconds": _round(total_duration),
            "errorCount": tool_errors.get(name, 0),
        })

    return stats, total_tool_calls, total_errors


def _build_category_breakdown(tool_records: list[dict]) -> list[dict]:
    category_counts = Counter(_classify_tool(record["name"]) for record in tool_records)
    category_durations: dict[str, float] = defaultdict(float)
    for record in tool_records:
        if record["start"] and record["end"]:
            category = _classify_tool(record["name"])
            category_durations[category] += (parse_timestamp(record["end"]) - parse_timestamp(record["start"])).total_seconds()

    return [
        {
            "category": category,
            "count": count,
            "totalDurationSeconds": _round(category_durations.get(category, 0.0)),
        }
        for category, count in category_counts.most_common()
    ]


def _build_hourly_activity(tool_records: list[dict]) -> list[dict]:
    hour_counts = Counter()
    for record in tool_records:
        if record["start"]:
            hour_counts[parse_timestamp(record["start"]).hour] += 1
    return [{"hour": hour, "count": hour_counts.get(hour, 0)} for hour in range(24)]


def _extract_shutdown_usage(events: list[dict]) -> dict:
    shutdown_events = [event for event in events if _event_type(event) == "session.shutdown"]
    if not shutdown_events:
        return {
            "input": 0,
            "output": sum(_coerce_int(_event_data(event).get("outputTokens")) for event in events if _event_type(event) == "assistant.message"),
            "cacheRead": 0,
        }

    shutdown = _event_data(shutdown_events[-1])
    model_metrics = _as_record(shutdown.get("modelMetrics"))
    total_input = 0
    total_output = 0
    total_cache = 0
    for metrics in model_metrics.values():
        usage = _as_record(_as_record(metrics).get("usage"))
        total_input += _coerce_int(usage.get("inputTokens"))
        total_output += _coerce_int(usage.get("outputTokens"))
        total_cache += _coerce_int(usage.get("cacheReadTokens"))

    return {
        "input": total_input,
        "output": total_output,
        "cacheRead": total_cache,
    }


def _extract_slice_usage(events: list[dict], parent_tool_call_id: str) -> dict:
    output_tokens = sum(
        _coerce_int(_event_data(event).get("outputTokens"))
        for event in events
        if _event_type(event) == "assistant.message" and _event_data(event).get("parentToolCallId") == parent_tool_call_id
    )

    task_completion = next((
        event for event in reversed(events)
        if _event_type(event) == "tool.execution_complete" and _event_data(event).get("toolCallId") == parent_tool_call_id
    ), None)
    task_metrics = _as_record(_as_record(_event_data(task_completion).get("toolTelemetry")).get("metrics")) if task_completion else {}

    subagent_completion = next((
        event for event in reversed(events)
        if _event_type(event) in {"subagent.completed", "subagent.failed"} and _event_data(event).get("toolCallId") == parent_tool_call_id
    ), None)
    subagent_data = _event_data(subagent_completion)

    input_tokens = _coerce_int(task_metrics.get("inputTokens"))
    explicit_output_tokens = _coerce_int(task_metrics.get("outputTokens"))
    cache_read_tokens = _coerce_int(task_metrics.get("cacheReadTokens"))
    total_tokens = _coerce_int(task_metrics.get("totalTokens"))

    if explicit_output_tokens:
        output_tokens = explicit_output_tokens
    if not total_tokens:
        total_tokens = _coerce_int(subagent_data.get("totalTokens"))
    if not total_tokens and input_tokens:
        total_tokens = input_tokens + output_tokens
    if not total_tokens:
        total_tokens = output_tokens
    if not input_tokens:
        input_tokens = max(total_tokens - output_tokens, 0)

    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read_tokens,
    }


def _collect_session_metadata(events: list[dict]) -> dict:
    assistant_models = set()
    versions = set()
    git_branches = set()
    entrypoints = set()
    agent_names = set()

    for event in events:
        event_type = _event_type(event)
        data = _event_data(event)
        if event_type == "session.start":
            selected_model = data.get("selectedModel")
            if selected_model:
                assistant_models.add(str(selected_model))
            version = data.get("copilotVersion")
            if version:
                versions.add(str(version))
            context = _as_record(data.get("context"))
            branch = context.get("branch")
            if branch:
                git_branches.add(str(branch))
            producer = data.get("producer")
            if producer:
                entrypoints.add(str(producer))
        elif event_type == "session.shutdown":
            model_metrics = _as_record(data.get("modelMetrics"))
            assistant_models.update(str(model) for model in model_metrics.keys())
        elif event_type in {"subagent.started", "subagent.completed", "subagent.failed"}:
            agent_name = data.get("agentName")
            if agent_name:
                agent_names.add(str(agent_name))

    if not agent_names:
        agent_names.add("main")

    return {
        "assistantModels": sorted(assistant_models),
        "versions": sorted(versions),
        "gitBranches": sorted(git_branches),
        "entrypoints": sorted(entrypoints),
        "agentNames": sorted(agent_names),
    }


def _build_slowest_turns(tool_records: list[dict], period_start: str, period_end: str) -> list[dict]:
    if not period_start or not period_end:
        return []
    duration_seconds = (parse_timestamp(period_end) - parse_timestamp(period_start)).total_seconds()
    return [{
        "turnIndex": 1,
        "durationSeconds": _round(duration_seconds),
        "tools": [record["name"] for record in tool_records],
    }]


def build_analysis(session_path: Path, exercise: str | None = None) -> dict:
    events = load_events(session_path)
    if not events:
        raise ValueError(f"No events found in {session_path}")

    parent_tool_call_id = _find_exercise_task_call_id(events, exercise) if exercise else None
    if exercise and not parent_tool_call_id:
        raise ValueError(f"Could not find a Copilot exercise-worker task for exercise '{exercise}'")

    context_events = _filter_events_for_task(events, parent_tool_call_id) if parent_tool_call_id else events
    if not context_events:
        raise ValueError(f"No relevant events found in {session_path}")

    tool_records = _build_tool_records(events, parent_tool_call_id)
    tool_stats, total_tool_calls, total_errors = _build_tool_stats(tool_records)
    category_breakdown = _build_category_breakdown(tool_records)
    hourly_activity = _build_hourly_activity(tool_records)

    all_timestamps = [event.get("timestamp") for event in context_events if event.get("timestamp")]
    if not all_timestamps:
        raise ValueError(f"No timestamped events found in {session_path}")

    period_start = min(all_timestamps)
    period_end = max(all_timestamps)
    duration_seconds = (parse_timestamp(period_end) - parse_timestamp(period_start)).total_seconds()

    token_usage = _extract_slice_usage(events, parent_tool_call_id) if parent_tool_call_id else _extract_shutdown_usage(events)
    total_tokens = token_usage["input"] + token_usage["output"]
    error_rate = _round((total_errors / total_tool_calls) * 100, 1) if total_tool_calls else 0.0

    turn_count = 1 if context_events else 0
    session_id = session_path.parent.name
    if exercise:
        session_id = f"{session_id}::{exercise}"

    return {
        "sessionId": session_id,
        "sessionPath": str(session_path),
        "period": {
            "start": parse_timestamp(period_start).isoformat(),
            "end": parse_timestamp(period_end).isoformat(),
            "durationSeconds": _round(duration_seconds),
        },
        "eventCount": len(context_events),
        "turnCount": turn_count,
        "totalToolCalls": total_tool_calls,
        "totalErrors": total_errors,
        "errorRate": error_rate,
        "tokenUsage": {
            "input": token_usage["input"],
            "output": token_usage["output"],
            "cacheRead": token_usage["cacheRead"],
            "total": total_tokens,
        },
        "toolStats": tool_stats,
        "categoryBreakdown": category_breakdown,
        "hourlyActivity": hourly_activity,
        "slowestTurns": _build_slowest_turns(tool_records, period_start, period_end),
        "waitTime": {
            "averageSeconds": None,
            "medianSeconds": None,
            "maxSeconds": None,
            "totalSeconds": None,
        },
        "sessionMetadata": _collect_session_metadata(context_events + [events[0]]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze GitHub Copilot CLI session telemetry")
    parser.add_argument("session_file", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--exercise", help="Analyze only the Copilot exercise-worker subtree for the given exercise")
    args = parser.parse_args()

    analysis = build_analysis(args.session_file, exercise=args.exercise)
    if args.format == "json":
        print(json.dumps(analysis, ensure_ascii=False))
    else:
        print_text_report(analysis)


if __name__ == "__main__":
    main()
