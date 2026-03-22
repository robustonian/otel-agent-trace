#!/usr/bin/env python3
"""
Claude Code セッションログから記事用の分析データを抽出する。
OTelエクスポートなしで直接JSONLを分析。
"""

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from parse_session import load_events, build_turns, parse_timestamp, classify_tool


def analyze(session_path: Path):
    events = load_events(session_path)
    turns = build_turns(events)
    session_id = session_path.stem

    all_ts = [e.get("timestamp") for e in events if e.get("timestamp")]
    start = parse_timestamp(min(all_ts))
    end = parse_timestamp(max(all_ts))
    duration = end - start

    print("=" * 60)
    print(f"セッション分析: {session_id[:8]}...")
    print(f"期間: {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}")
    print(f"総時間: {duration}")
    print(f"イベント数: {len(events)}")
    print(f"ターン数: {len(turns)}")
    print("=" * 60)

    # --- ツール統計 ---
    all_tools = []
    tool_durations = defaultdict(list)
    tool_errors = Counter()
    for turn in turns:
        for tc in turn["tool_calls"]:
            all_tools.append(tc["name"])
            if tc["end"]:
                tc_start = parse_timestamp(tc["start"])
                tc_end = parse_timestamp(tc["end"])
                dur = (tc_end - tc_start).total_seconds()
                tool_durations[tc["name"]].append(dur)
            if tc["error"]:
                tool_errors[tc["name"]] += 1

    print(f"\n📊 ツール呼び出し統計 (計 {len(all_tools)} 回)")
    print("-" * 50)
    counts = Counter(all_tools)
    for name, cnt in counts.most_common():
        pct = cnt / len(all_tools) * 100
        durations = tool_durations.get(name, [])
        avg = sum(durations) / len(durations) if durations else 0
        total = sum(durations)
        errors = tool_errors.get(name, 0)
        err_str = f" ⚠️ {errors}errors" if errors else ""
        print(f"  {name:20s} {cnt:4d}回 ({pct:5.1f}%)  "
              f"平均{avg:6.1f}s  合計{total:7.1f}s{err_str}")

    # --- カテゴリ別 ---
    print(f"\n📂 カテゴリ別")
    print("-" * 50)
    cat_counts = Counter(classify_tool(t) for t in all_tools)
    cat_durations = defaultdict(float)
    for name, durs in tool_durations.items():
        cat_durations[classify_tool(name)] += sum(durs)
    for cat, cnt in cat_counts.most_common():
        total_dur = cat_durations.get(cat, 0)
        print(f"  {cat:15s} {cnt:4d}回  合計{total_dur:7.1f}s")

    # --- トークン使用量 ---
    total_input = sum(t["total_input_tokens"] for t in turns)
    total_output = sum(t["total_output_tokens"] for t in turns)
    total_cache = sum(t.get("cache_read_tokens", 0) for t in turns)
    print(f"\n🔤 トークン使用量")
    print("-" * 50)
    print(f"  入力:     {total_input:>12,} tokens")
    print(f"  出力:     {total_output:>12,} tokens")
    print(f"  キャッシュ: {total_cache:>12,} tokens")
    print(f"  合計:     {total_input + total_output:>12,} tokens")

    # --- 時間帯別アクティビティ ---
    print(f"\n🕐 時間帯別ツール呼び出し")
    print("-" * 50)
    hour_counts = Counter()
    for turn in turns:
        for tc in turn["tool_calls"]:
            ts = parse_timestamp(tc["start"])
            hour_counts[ts.hour] += 1
    for hour in range(24):
        cnt = hour_counts.get(hour, 0)
        bar = "█" * (cnt // 2)
        if cnt:
            print(f"  {hour:02d}:00  {cnt:3d} {bar}")

    # --- 最も時間がかかったターン TOP10 ---
    print(f"\n🐌 最も時間がかかったターン TOP10")
    print("-" * 50)
    turn_durations = []
    for i, turn in enumerate(turns):
        ts = parse_timestamp(turn["start"])
        te = parse_timestamp(turn["end"])
        dur = (te - ts).total_seconds()
        tools = [tc["name"] for tc in turn["tool_calls"]]
        turn_durations.append((dur, i + 1, tools))
    turn_durations.sort(reverse=True)
    for dur, idx, tools in turn_durations[:10]:
        tools_str = ", ".join(tools) or "(thinking)"
        print(f"  Turn {idx:3d}: {dur:7.1f}s  [{tools_str}]")

    # --- エラー率 ---
    total_errors = sum(tool_errors.values())
    print(f"\n❌ エラー統計")
    print("-" * 50)
    print(f"  エラー数: {total_errors} / {len(all_tools)} ({total_errors/len(all_tools)*100:.1f}%)")
    for name, cnt in tool_errors.most_common():
        print(f"    {name}: {cnt}回")

    # --- ターン間の待機時間（ユーザーの思考時間） ---
    print(f"\n⏱️ ターン間待機時間 (ユーザー思考時間)")
    print("-" * 50)
    wait_times = []
    for i in range(1, len(turns)):
        prev_end = parse_timestamp(turns[i-1]["end"])
        curr_start = parse_timestamp(turns[i]["start"])
        wait = (curr_start - prev_end).total_seconds()
        if 0 < wait < 3600:  # 1時間以上は離席とみなす
            wait_times.append(wait)
    if wait_times:
        avg_wait = sum(wait_times) / len(wait_times)
        print(f"  平均: {avg_wait:.1f}s")
        print(f"  中央値: {sorted(wait_times)[len(wait_times)//2]:.1f}s")
        print(f"  最大: {max(wait_times):.1f}s")
        print(f"  合計待機: {sum(wait_times)/60:.1f}分")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # デフォルトで全seeda-corpセッションを分析
        base = Path.home() / ".claude/projects/-home-yuto-seeda-corp"
        for f in sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_size, reverse=True)[:3]:
            analyze(f)
            print("\n")
    else:
        analyze(Path(sys.argv[1]))
