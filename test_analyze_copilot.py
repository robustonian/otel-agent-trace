import json
from pathlib import Path

from analyze_copilot import build_analysis


EVENT_FIXTURE = [
    {
        "type": "session.start",
        "timestamp": "2026-03-30T10:00:00Z",
        "data": {
            "sessionId": "session-a",
            "copilotVersion": "1.0.12",
            "selectedModel": "gpt-5-mini",
            "producer": "copilot-agent",
            "context": {"cwd": "/tmp/fixture", "branch": "main"},
        },
    },
    {"type": "user.message", "timestamp": "2026-03-30T10:00:01Z", "data": {"content": "solve"}},
    {"type": "assistant.turn_start", "timestamp": "2026-03-30T10:00:02Z", "data": {"turnId": "0"}},
    {
        "type": "tool.execution_start",
        "timestamp": "2026-03-30T10:00:03Z",
        "data": {
            "toolCallId": "task-acronym",
            "toolName": "task",
            "arguments": {
                "description": "Solve acronym",
                "agent_type": "exercise-worker",
                "prompt": "exercise acronym",
            },
        },
    },
    {
        "type": "subagent.started",
        "timestamp": "2026-03-30T10:00:04Z",
        "data": {"toolCallId": "task-acronym", "agentName": "exercise-worker"},
    },
    {
        "type": "assistant.message",
        "timestamp": "2026-03-30T10:00:05Z",
        "data": {
            "parentToolCallId": "task-acronym",
            "messageId": "m1",
            "content": "",
            "toolRequests": [],
            "outputTokens": 30,
        },
    },
    {
        "type": "tool.execution_start",
        "timestamp": "2026-03-30T10:00:06Z",
        "data": {
            "parentToolCallId": "task-acronym",
            "toolCallId": "view-acronym",
            "toolName": "view",
            "arguments": {"path": "/tmp/fixture/acronym.ts"},
        },
    },
    {
        "type": "tool.execution_complete",
        "timestamp": "2026-03-30T10:00:07Z",
        "data": {
            "parentToolCallId": "task-acronym",
            "toolCallId": "view-acronym",
            "success": True,
        },
    },
    {
        "type": "tool.execution_start",
        "timestamp": "2026-03-30T10:00:08Z",
        "data": {
            "parentToolCallId": "task-acronym",
            "toolCallId": "bash-acronym",
            "toolName": "bash",
            "arguments": {"command": "yarn test"},
        },
    },
    {
        "type": "tool.execution_complete",
        "timestamp": "2026-03-30T10:00:09Z",
        "data": {
            "parentToolCallId": "task-acronym",
            "toolCallId": "bash-acronym",
            "success": True,
            "result": {
                "content": "command failed\n<exited with exit code 1>",
                "detailedContent": "command failed\n<exited with exit code 1>",
            },
        },
    },
    {
        "type": "assistant.message",
        "timestamp": "2026-03-30T10:00:10Z",
        "data": {
            "parentToolCallId": "task-acronym",
            "messageId": "m2",
            "content": "done",
            "toolRequests": [],
            "outputTokens": 20,
        },
    },
    {
        "type": "subagent.completed",
        "timestamp": "2026-03-30T10:00:11Z",
        "data": {
            "toolCallId": "task-acronym",
            "agentName": "exercise-worker",
            "totalTokens": 120,
        },
    },
    {
        "type": "tool.execution_complete",
        "timestamp": "2026-03-30T10:00:12Z",
        "data": {
            "toolCallId": "task-acronym",
            "success": True,
            "toolTelemetry": {"metrics": {"numberOfToolCallsMadeByAgent": 2}},
        },
    },
    {
        "type": "tool.execution_start",
        "timestamp": "2026-03-30T10:00:13Z",
        "data": {
            "toolCallId": "task-anagram",
            "toolName": "task",
            "arguments": {
                "description": "Solve anagram",
                "agent_type": "exercise-worker",
                "prompt": "exercise anagram",
            },
        },
    },
    {
        "type": "subagent.started",
        "timestamp": "2026-03-30T10:00:14Z",
        "data": {"toolCallId": "task-anagram", "agentName": "exercise-worker"},
    },
    {
        "type": "assistant.message",
        "timestamp": "2026-03-30T10:00:15Z",
        "data": {
            "parentToolCallId": "task-anagram",
            "messageId": "m3",
            "content": "done",
            "toolRequests": [],
            "outputTokens": 30,
        },
    },
    {
        "type": "tool.execution_start",
        "timestamp": "2026-03-30T10:00:16Z",
        "data": {
            "parentToolCallId": "task-anagram",
            "toolCallId": "view-anagram",
            "toolName": "view",
            "arguments": {"path": "/tmp/fixture/anagram.ts"},
        },
    },
    {
        "type": "tool.execution_complete",
        "timestamp": "2026-03-30T10:00:17Z",
        "data": {
            "parentToolCallId": "task-anagram",
            "toolCallId": "view-anagram",
            "success": True,
        },
    },
    {
        "type": "subagent.completed",
        "timestamp": "2026-03-30T10:00:18Z",
        "data": {
            "toolCallId": "task-anagram",
            "agentName": "exercise-worker",
            "totalTokens": 80,
        },
    },
    {
        "type": "tool.execution_complete",
        "timestamp": "2026-03-30T10:00:19Z",
        "data": {"toolCallId": "task-anagram", "success": True},
    },
    {
        "type": "assistant.message",
        "timestamp": "2026-03-30T10:00:20Z",
        "data": {
            "messageId": "m4",
            "content": "batch done",
            "toolRequests": [],
            "outputTokens": 10,
        },
    },
    {"type": "assistant.turn_end", "timestamp": "2026-03-30T10:00:21Z", "data": {"turnId": "0"}},
    {
        "type": "session.shutdown",
        "timestamp": "2026-03-30T10:00:22Z",
        "data": {
            "modelMetrics": {
                "gpt-5-mini": {
                    "usage": {
                        "inputTokens": 220,
                        "outputTokens": 90,
                        "cacheReadTokens": 40,
                    }
                }
            }
        },
    },
]


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def test_build_analysis_uses_session_shutdown_usage(tmp_path: Path) -> None:
    session_dir = tmp_path / "session-a"
    session_dir.mkdir()
    session_file = session_dir / "events.jsonl"
    _write_events(session_file, EVENT_FIXTURE)

    analysis = build_analysis(session_file)

    assert analysis["tokenUsage"] == {
        "input": 220,
        "output": 90,
        "cacheRead": 40,
        "total": 310,
    }
    assert analysis["totalToolCalls"] == 5
    assert analysis["totalErrors"] == 1
    assert analysis["sessionMetadata"]["assistantModels"] == ["gpt-5-mini"]
    assert analysis["sessionMetadata"]["versions"] == ["1.0.12"]


def test_build_analysis_filters_to_exercise_worker_slice(tmp_path: Path) -> None:
    session_dir = tmp_path / "session-a"
    session_dir.mkdir()
    session_file = session_dir / "events.jsonl"
    _write_events(session_file, EVENT_FIXTURE)

    analysis = build_analysis(session_file, exercise="acronym")

    assert analysis["sessionId"].endswith("::acronym")
    assert analysis["tokenUsage"] == {
        "input": 70,
        "output": 50,
        "cacheRead": 0,
        "total": 120,
    }
    assert analysis["totalToolCalls"] == 3
    assert analysis["totalErrors"] == 1
    assert sorted(stat["name"] for stat in analysis["toolStats"]) == ["bash", "task", "view"]
