#!/usr/bin/env python3
"""
Claude Code セッションログから分析データを抽出する。
デフォルトはテキスト出力だが、機械可読なJSON出力にも対応する。
"""

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from parse_session import build_turns, classify_tool, load_events, parse_timestamp


def _round(value: float, digits: int = 3) -> float:
    return round(value, digits)


def _collect_session_metadata(events: list[dict]) -> dict:
    assistant_models = sorted({
        ev.get("message", {}).get("model")
        for ev in events
        if ev.get("type") == "assistant" and ev.get("message", {}).get("model")
    })
    versions = sorted({ev.get("version") for ev in events if ev.get("version")})
    git_branches = sorted({ev.get("gitBranch") for ev in events if ev.get("gitBranch")})
    entrypoints = sorted({ev.get("entrypoint") for ev in events if ev.get("entrypoint")})
    agent_names = sorted({ev.get("agentName") for ev in events if ev.get("agentName")})

    return {
        "assistantModels": assistant_models,
        "versions": versions,
        "gitBranches": git_branches,
        "entrypoints": entrypoints,
        "agentNames": agent_names,
    }


def build_analysis(session_path: Path) -> dict:
    events = load_events(session_path)
    turns = build_turns(events)
    session_id = session_path.stem

    all_ts = [e.get("timestamp") for e in events if e.get("timestamp")]
    if not all_ts:
        raise ValueError(f"No timestamped events found in {session_path}")

    start = parse_timestamp(min(all_ts))
    end = parse_timestamp(max(all_ts))
    duration_seconds = (end - start).total_seconds()

    all_tools: list[str] = []
    tool_durations: dict[str, list[float]] = defaultdict(list)
    tool_errors: Counter[str] = Counter()

    for turn in turns:
        for tc in turn["tool_calls"]:
            all_tools.append(tc["name"])
            if tc["end"]:
                tc_start = parse_timestamp(tc["start"])
                tc_end = parse_timestamp(tc["end"])
                tool_durations[tc["name"]].append((tc_end - tc_start).total_seconds())
            if tc["error"]:
                tool_errors[tc["name"]] += 1

    tool_counts = Counter(all_tools)
    total_tool_calls = len(all_tools)
    tool_stats = []
    for name, count in tool_counts.most_common():
        durations = tool_durations.get(name, [])
        avg_duration = sum(durations) / len(durations) if durations else 0
        total_duration = sum(durations)
        tool_stats.append({
            "name": name,
            "category": classify_tool(name),
            "count": count,
            "percentage": _round((count / total_tool_calls) * 100, 1) if total_tool_calls else 0.0,
            "avgDurationSeconds": _round(avg_duration),
            "totalDurationSeconds": _round(total_duration),
            "errorCount": tool_errors.get(name, 0),
        })

    category_counts = Counter(classify_tool(name) for name in all_tools)
    category_durations: dict[str, float] = defaultdict(float)
    for name, durations in tool_durations.items():
        category_durations[classify_tool(name)] += sum(durations)
    category_breakdown = [
        {
            "category": category,
            "count": count,
            "totalDurationSeconds": _round(category_durations.get(category, 0.0)),
        }
        for category, count in category_counts.most_common()
    ]

    total_input = sum(turn["total_input_tokens"] for turn in turns)
    total_output = sum(turn["total_output_tokens"] for turn in turns)
    total_cache = sum(turn.get("cache_read_tokens", 0) for turn in turns)

    hour_counts = Counter()
    for turn in turns:
        for tc in turn["tool_calls"]:
            ts = parse_timestamp(tc["start"])
            hour_counts[ts.hour] += 1
    hourly_activity = [{"hour": hour, "count": hour_counts.get(hour, 0)} for hour in range(24)]

    turn_durations = []
    for index, turn in enumerate(turns):
        ts = parse_timestamp(turn["start"])
        te = parse_timestamp(turn["end"])
        turn_durations.append({
            "turnIndex": index + 1,
            "durationSeconds": _round((te - ts).total_seconds()),
            "tools": [tc["name"] for tc in turn["tool_calls"]],
        })
    slowest_turns = sorted(
        turn_durations,
        key=lambda turn: turn["durationSeconds"],
        reverse=True,
    )[:10]

    wait_times = []
    for index in range(1, len(turns)):
        prev_end = parse_timestamp(turns[index - 1]["end"])
        curr_start = parse_timestamp(turns[index]["start"])
        wait = (curr_start - prev_end).total_seconds()
        if 0 < wait < 3600:
            wait_times.append(wait)

    wait_summary = {
        "averageSeconds": _round(sum(wait_times) / len(wait_times), 1) if wait_times else None,
        "medianSeconds": _round(statistics.median(wait_times), 1) if wait_times else None,
        "maxSeconds": _round(max(wait_times), 1) if wait_times else None,
        "totalSeconds": _round(sum(wait_times), 1) if wait_times else None,
    }

    total_errors = sum(tool_errors.values())
    error_rate = _round((total_errors / total_tool_calls) * 100, 1) if total_tool_calls else 0.0

    return {
        "sessionId": session_id,
        "sessionPath": str(session_path),
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "durationSeconds": _round(duration_seconds),
        },
        "eventCount": len(events),
        "turnCount": len(turns),
        "totalToolCalls": total_tool_calls,
        "totalErrors": total_errors,
        "errorRate": error_rate,
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
        "sessionMetadata": _collect_session_metadata(events),
    }


def print_text_report(analysis: dict) -> None:
    period = analysis["period"]
    start = parse_timestamp(period["start"])
    end = parse_timestamp(period["end"])
    duration = timedelta(seconds=period["durationSeconds"])

    print("=" * 60)
    print(f"セッション分析: {analysis['sessionId'][:8]}...")
    print(f"期間: {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}")
    print(f"総時間: {duration}")
    print(f"イベント数: {analysis['eventCount']}")
    print(f"ターン数: {analysis['turnCount']}")
    print("=" * 60)

    print(f"\n📊 ツール呼び出し統計 (計 {analysis['totalToolCalls']} 回)")
    print("-" * 50)
    for stat in analysis["toolStats"]:
        err_str = f" ⚠️ {stat['errorCount']}errors" if stat["errorCount"] else ""
        print(
            f"  {stat['name']:20s} {stat['count']:4d}回 ({stat['percentage']:5.1f}%)  "
            f"平均{stat['avgDurationSeconds']:6.1f}s  合計{stat['totalDurationSeconds']:7.1f}s{err_str}"
        )

    print(f"\n📂 カテゴリ別")
    print("-" * 50)
    for category in analysis["categoryBreakdown"]:
        print(
            f"  {category['category']:15s} {category['count']:4d}回  "
            f"合計{category['totalDurationSeconds']:7.1f}s"
        )

    token_usage = analysis["tokenUsage"]
    print(f"\n🔤 トークン使用量")
    print("-" * 50)
    print(f"  入力:     {token_usage['input']:>12,} tokens")
    print(f"  出力:     {token_usage['output']:>12,} tokens")
    print(f"  キャッシュ: {token_usage['cacheRead']:>12,} tokens")
    print(f"  合計:     {token_usage['total']:>12,} tokens")

    print(f"\n🕐 時間帯別ツール呼び出し")
    print("-" * 50)
    for entry in analysis["hourlyActivity"]:
        if entry["count"]:
            bar = "█" * (entry["count"] // 2)
            print(f"  {entry['hour']:02d}:00  {entry['count']:3d} {bar}")

    print(f"\n🐌 最も時間がかかったターン TOP10")
    print("-" * 50)
    for turn in analysis["slowestTurns"]:
        tools = ", ".join(turn["tools"]) or "(thinking)"
        print(f"  Turn {turn['turnIndex']:3d}: {turn['durationSeconds']:7.1f}s  [{tools}]")

    print(f"\n❌ エラー統計")
    print("-" * 50)
    print(
        f"  エラー数: {analysis['totalErrors']} / {analysis['totalToolCalls']} "
        f"({analysis['errorRate']:.1f}%)"
    )
    for stat in analysis["toolStats"]:
        if stat["errorCount"]:
            print(f"    {stat['name']}: {stat['errorCount']}回")

    print(f"\n⏱️ ターン間待機時間 (ユーザー思考時間)")
    print("-" * 50)
    wait_time = analysis["waitTime"]
    if wait_time["averageSeconds"] is not None:
        print(f"  平均: {wait_time['averageSeconds']:.1f}s")
        print(f"  中央値: {wait_time['medianSeconds']:.1f}s")
        print(f"  最大: {wait_time['maxSeconds']:.1f}s")
        print(f"  合計待機: {wait_time['totalSeconds'] / 60:.1f}分")


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
        description="Claude Code session log analysis"
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

    if args.session_file:
        analyze(args.session_file, args.format, args.pretty_json)
        return

    if args.format == "json":
        parser.error("JSON output requires an explicit session_file path")

    base = Path.home() / ".claude/projects/-home-yuto-seeda-corp"
    for session_file in sorted(base.glob("*.jsonl"), key=lambda path: path.stat().st_size, reverse=True)[:3]:
        analyze(session_file, args.format, args.pretty_json)
        print("\n")


if __name__ == "__main__":
    main()
