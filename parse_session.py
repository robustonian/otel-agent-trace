#!/usr/bin/env python3
"""
Claude Code セッションログ (.jsonl) → OpenTelemetry トレース変換

Span階層:
  session (root)
  └─ turn (ユーザーメッセージ → アシスタント応答)
     └─ tool_call (Bash, Read, Write, Edit, ...)

記録する属性:
  - ツール名、実行時間
  - トークン使用量 (input/output)
  - 成功/失敗
  - サブエージェント情報
  ※ 会話テキスト・ファイルパス・コマンド内容は意図的に除外 (セキュリティ)
"""

import json
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import StatusCode


def parse_timestamp(ts_str: str) -> datetime:
    """ISO 8601 タイムスタンプをパース"""
    ts = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(ts)


def estimate_content_size(content) -> int:
    """コンテンツのおおよそのサイズ（文字数）を返す。中身は記録しない"""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(estimate_content_size(c) for c in content)
    if isinstance(content, dict):
        return sum(estimate_content_size(v) for v in content.values())
    return 0


def classify_tool(name: str) -> str:
    """ツールをカテゴリに分類"""
    categories = {
        "file_read": ["Read", "Glob", "Grep"],
        "file_write": ["Write", "Edit"],
        "shell": ["Bash"],
        "web": ["WebSearch", "WebFetch"],
        "agent": ["Agent", "TaskOutput"],
        "system": ["ToolSearch", "EnterPlanMode", "ExitPlanMode",
                    "AskUserQuestion", "NotebookEdit"],
    }
    for cat, tools in categories.items():
        if name in tools:
            return cat
    return "other"


def load_events(path: Path) -> list[dict]:
    """JSONLを読み込み、タイムスタンプでソート"""
    events = []
    with open(path) as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  skip line {line_no}: invalid JSON", file=sys.stderr)
    events.sort(key=lambda e: e.get("timestamp", ""))
    return events


def build_turns(events: list[dict]) -> list[dict]:
    """
    イベント列をターン（ユーザー発言→アシスタント応答）に構造化する。
    各ターンは tool_call のリストを持つ。
    """
    turns = []
    current_turn = None

    for ev in events:
        ev_type = ev.get("type")
        ts = ev.get("timestamp")
        if not ts:
            continue

        if ev_type == "user":
            # userメッセージ内のtool_resultで前ターンのツール完了時刻を記録
            msg = ev.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list) and current_turn:
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        tool_use_id = c.get("tool_use_id", "")
                        for tc in reversed(current_turn["tool_calls"]):
                            if tc["id"] == tool_use_id:
                                tc["end"] = ts
                                tc["output_size"] = estimate_content_size(
                                    c.get("content", "")
                                )
                                if c.get("is_error"):
                                    tc["error"] = True
                                break

            # 新しいターン開始
            if current_turn:
                turns.append(current_turn)
            current_turn = {
                "start": ts,
                "end": ts,
                "user_content_size": estimate_content_size(content),
                "tool_calls": [],
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "agent_name": ev.get("agentName"),
                "team_name": ev.get("teamName"),
            }

        elif ev_type == "assistant" and current_turn:
            current_turn["end"] = ts
            msg = ev.get("message", {})
            usage = msg.get("usage", {})
            current_turn["total_input_tokens"] += usage.get("input_tokens", 0)
            current_turn["total_output_tokens"] += usage.get("output_tokens", 0)
            cache_read = usage.get("cache_read_input_tokens", 0)
            if cache_read:
                current_turn.setdefault("cache_read_tokens", 0)
                current_turn["cache_read_tokens"] += cache_read

            for c in msg.get("content", []):
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    current_turn["tool_calls"].append({
                        "id": c.get("id", ""),
                        "name": c.get("name", "unknown"),
                        "start": ts,
                        "end": None,  # tool_resultで埋める
                        "input_size": estimate_content_size(c.get("input", {})),
                        "output_size": 0,
                        "error": False,
                    })

        elif ev_type == "tool_result" and current_turn:
            # ツールの結果 → 対応する tool_call の end を埋める
            tool_use_id = ev.get("toolUseID") or ev.get("tool_use_id")
            for tc in reversed(current_turn["tool_calls"]):
                if tc["id"] == tool_use_id:
                    tc["end"] = ts
                    content = ev.get("content", "")
                    tc["output_size"] = estimate_content_size(content)
                    if ev.get("is_error"):
                        tc["error"] = True
                    break

        # progress イベントからもツール完了を拾う
        elif ev_type == "progress" and current_turn:
            current_turn["end"] = ts

    if current_turn:
        turns.append(current_turn)

    return turns


def export_traces(session_path: Path, endpoint: str, dry_run: bool = False):
    """セッションログをOTelトレースとしてエクスポート"""
    print(f"Loading: {session_path}")
    events = load_events(session_path)
    print(f"  Events: {len(events)}")

    session_id = session_path.stem
    turns = build_turns(events)
    print(f"  Turns: {len(turns)}")
    total_tools = sum(len(t["tool_calls"]) for t in turns)
    print(f"  Tool calls: {total_tools}")

    if dry_run:
        print("\n=== Dry Run Summary ===")
        for i, turn in enumerate(turns):
            tools = ", ".join(tc["name"] for tc in turn["tool_calls"]) or "(no tools)"
            print(f"  Turn {i+1}: {len(turn['tool_calls'])} tools [{tools}]")
        return

    # OTel セットアップ
    resource = Resource.create({
        "service.name": "claude-code",
        "service.version": "1.0.0",
        "session.id": session_id,
    })
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("claude-code-session")

    # セッション全体の時間範囲
    all_ts = [e.get("timestamp") for e in events if e.get("timestamp")]
    session_start = parse_timestamp(min(all_ts))
    session_end = parse_timestamp(max(all_ts))

    # タイムスタンプを「今」に寄せる（retention period対策）
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    time_offset = now - session_end - timedelta(minutes=5)
    session_start += time_offset
    session_end += time_offset
    print(f"  Time offset: {time_offset} (shifted to near-now)")

    def shift_ts(ts_str):
        return parse_timestamp(ts_str) + time_offset

    # Root span: セッション（context managerを使わず手動管理で二重end回避）
    session_ctx = tracer.start_span(
        name="session",
        start_time=int(session_start.timestamp() * 1e9),
        attributes={
            "session.id": session_id,
            "session.turns": len(turns),
            "session.tool_calls": total_tools,
        },
    )
    session_token = trace.context_api.attach(
        trace.set_span_in_context(session_ctx)
    )

    try:
        for i, turn in enumerate(turns):
            turn_start = shift_ts(turn["start"])
            turn_end = shift_ts(turn["end"])

            turn_span = tracer.start_span(
                name=f"turn-{i+1}",
                start_time=int(turn_start.timestamp() * 1e9),
                attributes={
                    "turn.index": i + 1,
                    "turn.tool_count": len(turn["tool_calls"]),
                    "turn.input_tokens": turn["total_input_tokens"],
                    "turn.output_tokens": turn["total_output_tokens"],
                    "turn.cache_read_tokens": turn.get("cache_read_tokens", 0),
                    "turn.user_content_size": turn["user_content_size"],
                    "turn.agent_name": turn.get("agent_name") or "main",
                    "turn.team_name": turn.get("team_name") or "",
                },
            )
            turn_token = trace.context_api.attach(
                trace.set_span_in_context(turn_span)
            )

            for tc in turn["tool_calls"]:
                tc_start = shift_ts(tc["start"])
                tc_end = shift_ts(tc["end"]) if tc["end"] else turn_end

                tool_span = tracer.start_span(
                    name=f"tool:{tc['name']}",
                    start_time=int(tc_start.timestamp() * 1e9),
                    attributes={
                        "tool.name": tc["name"],
                        "tool.category": classify_tool(tc["name"]),
                        "tool.input_size": tc["input_size"],
                        "tool.output_size": tc["output_size"],
                        "tool.error": tc["error"],
                    },
                )
                if tc["error"]:
                    tool_span.set_status(StatusCode.ERROR, "Tool execution failed")
                tool_span.end(end_time=int(tc_end.timestamp() * 1e9))

            trace.context_api.detach(turn_token)
            turn_span.end(end_time=int(turn_end.timestamp() * 1e9))
    finally:
        trace.context_api.detach(session_token)
        session_ctx.end(end_time=int(session_end.timestamp() * 1e9))

    provider.force_flush()
    provider.shutdown()
    print(f"\n✅ Exported {len(turns)} turns, {total_tools} tool calls to {endpoint}")


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code session log → OpenTelemetry traces"
    )
    parser.add_argument("session_file", type=Path, help="Path to .jsonl session file")
    parser.add_argument(
        "--endpoint", default="localhost:4317", help="OTLP gRPC endpoint"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Parse only, don't export"
    )
    args = parser.parse_args()
    export_traces(args.session_file, args.endpoint, args.dry_run)


if __name__ == "__main__":
    main()
