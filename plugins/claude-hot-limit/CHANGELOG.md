# Changelog

## 1.0.1

- docs：加入 HOT LIMIT 命名彩蛋（致敬 T.M.Revolution 1998 同名單曲）。README footnote + CLAUDE.md Purpose；無功能變動。

## 1.0.0

初版。

- **pacing-guard** PreToolUse hook：守 `Workflow`/`Agent` fan-out 啟動節奏。
  - Burst guard：滾動窗口內啟動數超上限 → deny。
  - Min-gap：兩發太近 → 自動 sleep（防 short-burst）。
  - fail-open、flock 序列化、`$CLAUDE_PLUGIN_DATA` 帳本、env + 檔案旗標 override。
  - 8/8 本地 RED/GREEN 測試通過。
- **pacing-playbook** skill：設計期反 burst 引導 + 決策檢查表。
