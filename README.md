# otel-agent-trace

Claude Code のセッションログ（JSONL）を OpenTelemetry トレースに変換し、Jaeger で可視化するツール。

**関連記事**: [Claude Codeの動きをOpenTelemetryで可視化したら「何してたか分からない」が消えた](https://zenn.dev/seeda_yuto/articles/otel-ai-agent-observability)

## 概要

AI エージェントの「何をやっているか分からない」問題を、Web サービスと同じ可観測性の手法で解決する。

- セッションログを `session > turn > tool_call` の 3 層 Span に変換
- ツール名・カテゴリ・実行時間・エラー有無を記録（会話テキストは記録しない）
- Jaeger のウォーターフォールでボトルネックを一目で把握

## セットアップ

```bash
# 1. Jaeger を起動
docker compose up -d
# → http://localhost:16686 で Jaeger UI が開く

# 2. Python 依存をインストール
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 使い方

### トレースをエクスポート

```bash
# Claude Code のセッションログを OTel トレースに変換・送信
python parse_session.py ~/.claude/projects/<project>/session.jsonl

# エンドポイントを指定（デフォルト: localhost:4317）
python parse_session.py session.jsonl --endpoint localhost:14317

# パースだけ（エクスポートしない）
python parse_session.py session.jsonl --dry-run
```

### セッションを分析（OTel なし）

#### Claude Code セッション

```bash
# 指定したセッションを分析
python analyze_session.py session.jsonl

# JSONで機械可読出力
python analyze_session.py session.jsonl --format json

# JSONを整形して出力
python analyze_session.py session.jsonl --format json --pretty-json

# 引数なしで直近の大きいセッションを自動分析
python analyze_session.py
```

#### vibe-local セッション

```bash
# telemetry summary を含む vibe-local セッションを分析
python analyze_vibe_local.py session.jsonl

# JSONで機械可読出力
python analyze_vibe_local.py session.jsonl --format json

# JSONを整形して出力
python analyze_vibe_local.py session.jsonl --format json --pretty-json
```

`analyze_vibe_local.py` は、通常の chat message 行に加えて `record_type` を持つ
非メッセージ telemetry record が JSONL に混在している前提で動作する。
`vibe_local_telemetry` の state record を優先して読み取り、必要なら summary 互換 payload
にもフォールバックする。telemetry record が見つからない場合は、新しい telemetry
形式のセッションが必要であることを示すエラーメッセージを返す。

#### GitHub Copilot CLI セッション

```bash
# Copilot CLI の events.jsonl を分析
python analyze_copilot.py ~/.copilot/session-state/<session-id>/events.jsonl

# JSONで機械可読出力
python analyze_copilot.py ~/.copilot/session-state/<session-id>/events.jsonl --format json

# one-prompt バッチのうち特定 exercise-worker だけを分析
python analyze_copilot.py ~/.copilot/session-state/<session-id>/events.jsonl --format json --exercise acronym
```

`analyze_copilot.py` は Copilot CLI の `events.jsonl` を読み取り、全体セッションでは
`session.shutdown.modelMetrics` を使って token usage を集計する。`--exercise` を渡した
場合は、対応する `Solve <exercise>` task / `exercise-worker` subtree に絞って、ツール呼び出し、
エラー、duration、worker-scope token usage を既存スキーマへ正規化する。cache read は
その scope で明示的に出てきた場合だけ別計上し、見えていない値は推測しない。

出力例:

```
📊 ツール呼び出し統計 (計 130 回)
  Bash           90回 (69.2%)  平均 13.8s  合計 1,185s  ⚠️ 9 errors
  TaskOutput     20回 (15.4%)  平均 79.2s  合計 1,506s  ⚠️ 5 errors
  Agent           6回 ( 4.6%)  平均 50.4s  合計   252s
  Read            6回 ( 4.6%)  平均  0.0s  合計     0s
```

JSON出力には以下が含まれる:

- セッション期間、イベント数、ターン数
- ツール別集計（回数、比率、平均/合計時間、エラー数）
- カテゴリ別集計
- トークン使用量
- 時間帯別アクティビティ
- 最遅ターン一覧
- ターン間待機時間
- セッションメタデータ（CLIバージョン、entrypoint、assistant model など）

この JSON は `ts-bench` など別ツールから機械的に取り込む用途を想定している。

`parse_session.py --dry-run` は OpenTelemetry 依存なしで動作し、セッション構造の確認だけを行える。

## Span 設計

```
session (root)
└─ turn (ユーザーの1発言 → AIの応答)
   └─ tool_call (Bash, Read, Write, WebSearch, ...)
```

### 記録する属性

| Span | 属性 |
|------|------|
| session | `session.id`, `session.turns`, `session.tool_calls` |
| turn | `turn.index`, `turn.tool_count`, `turn.input_tokens`, `turn.output_tokens`, `turn.cache_read_tokens`, `turn.agent_name` |
| tool_call | `tool.name`, `tool.category`, `tool.input_size`, `tool.output_size`, `tool.error` |

### 意図的に記録しないもの

- 会話テキスト（業務情報が含まれる）
- ファイルパス（プロジェクト構造が漏れる）
- コマンド内容（認証情報が含まれうる）

## ライセンス

MIT
