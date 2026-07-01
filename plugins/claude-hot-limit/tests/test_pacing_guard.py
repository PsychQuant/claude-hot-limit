#!/usr/bin/env python3
"""
claude-hot-limit · pacing-guard 黑箱行為測試

把 hook 當 CLI 黑箱測：餵 stdin JSON + env，檢查 stdout（deny / 放行）與 ledger。
不碰內部函式，故重構不破測試。stdlib unittest，無外部相依。

跑法:
    python3 -m unittest discover -s tests
    python3 tests/test_pacing_guard.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "pacing-guard.py")


def run_hook(tool="Workflow", env_overrides=None, payload=None):
    """跑一次 hook 行程，回傳 (exit_code, parsed_stdout_or_None, raw_stdout)。"""
    if payload is None:
        payload = {"tool_name": tool, "tool_input": {}}
    # 乾淨 env：剝掉所有 CLAUDE_HOT_LIMIT_* / CLAUDE_PLUGIN_DATA，再套用 overrides
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_HOT_LIMIT_") and k != "CLAUDE_PLUGIN_DATA"}
    env.update(env_overrides or {})
    proc = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=30,
    )
    raw = proc.stdout.strip()
    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    return proc.returncode, parsed, raw


def is_deny(parsed):
    return bool(parsed) and \
        parsed.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def make_transcript(tmp_dir, models):
    """建一個假 transcript JSONL，依序寫入 assistant turn；None → <synthetic>（應被跳過）。回傳路徑。"""
    path = os.path.join(tmp_dir, "transcript-%s.jsonl" % len(models))
    with open(path, "w") as f:
        for i, m in enumerate(models):
            model_value = m if m is not None else "<synthetic>"
            f.write(json.dumps({"type": "assistant", "message": {"model": model_value}}) + "\n")
    return path


def last_ledger_row(data_dir):
    path = os.path.join(data_dir, "launches.jsonl")
    with open(path) as f:
        lines = [l for l in f if l.strip()]
    return json.loads(lines[-1]) if lines else None


class AccountLevelLedgerTest(unittest.TestCase):
    """核心：launch 計數必須是帳號級，跨安裝來源（CLAUDE_PLUGIN_DATA）共用一本帳。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.account_dir = os.path.join(self.tmp.name, "account")
        self.base = {
            "CLAUDE_HOT_LIMIT_DATA": self.account_dir,  # 帳號級帳本重導到 temp
            "CLAUDE_HOT_LIMIT_MAX": "3",
            "CLAUDE_HOT_LIMIT_WINDOW": "600",
            "CLAUDE_HOT_LIMIT_MIN_GAP": "0",            # 關 sleep，測試不卡時間
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _fire(self, install_source):
        """模擬某個安裝來源（不同 CLAUDE_PLUGIN_DATA）發一次 fan-out。"""
        src_dir = os.path.join(self.tmp.name, "install-" + install_source)
        env = dict(self.base, CLAUDE_PLUGIN_DATA=src_dir)
        return run_hook(tool="Workflow", env_overrides=env)

    def test_count_shared_across_install_sources(self):
        # 來源 A 連發 3 次（達上限），全部放行
        for _ in range(3):
            code, parsed, _ = self._fire("A")
            self.assertFalse(is_deny(parsed), "前 3 發不應被擋")
        # 第 4 發改走「來源 B」——帳號級計數已達 3，必須 deny
        code, parsed, raw = self._fire("B")
        self.assertTrue(
            is_deny(parsed),
            "跨安裝來源的第 4 發必須被擋（帳號級 burst guard），實際 stdout=%r" % raw,
        )


class GuardBehaviorTest(unittest.TestCase):
    """回歸安全網：鎖住既有行為（放行 / deny / off / disabled / 非 fan-out / min-gap）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "ledger")
        self.env = {"CLAUDE_HOT_LIMIT_DATA": self.data, "CLAUDE_HOT_LIMIT_MIN_GAP": "0"}

    def tearDown(self):
        self.tmp.cleanup()

    def fire(self, tool="Workflow", extra=None):
        env = dict(self.env, **(extra or {}))
        return run_hook(tool=tool, env_overrides=env)

    def test_first_launch_allowed(self):
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "3"})
        self.assertFalse(is_deny(parsed), "首發空帳本應放行，stdout=%r" % raw)

    def test_denied_when_over_max(self):
        for _ in range(2):
            _, parsed, _ = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "2"})
            self.assertFalse(is_deny(parsed))
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "2"})
        self.assertTrue(is_deny(parsed), "第 MAX+1 發應被擋，stdout=%r" % raw)

    def test_non_fanout_tool_not_counted(self):
        # Read 不是 fan-out 入口：應放行，且不消耗 budget
        for _ in range(5):
            _, parsed, _ = self.fire(tool="Read", extra={"CLAUDE_HOT_LIMIT_MAX": "3"})
            self.assertFalse(is_deny(parsed))
        # 連發 5 次 Read 後，第一發 Workflow 仍應放行（Read 沒被計數）
        _, parsed, raw = self.fire(tool="Workflow", extra={"CLAUDE_HOT_LIMIT_MAX": "3"})
        self.assertFalse(is_deny(parsed), "Read 不應消耗 fan-out budget，stdout=%r" % raw)

    def test_global_off_switch_overrides_burst(self):
        _, parsed, _ = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "1"})
        self.assertFalse(is_deny(parsed))
        # 達上限後，OFF=1 應強制放行
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "1", "CLAUDE_HOT_LIMIT_OFF": "1"})
        self.assertFalse(is_deny(parsed), "OFF=1 應 override burst，stdout=%r" % raw)

    def test_disabled_file_flag_allows(self):
        os.makedirs(self.data, exist_ok=True)
        open(os.path.join(self.data, "disabled"), "w").close()
        # 即使灌爆上限，disabled 旗標在就一律放行
        for _ in range(5):
            _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "1"})
            self.assertFalse(is_deny(parsed), "disabled 旗標應放行，stdout=%r" % raw)

    def test_min_gap_sleeps_and_reports(self):
        # MAX 放寬避免 burst deny；MIN_GAP=2 → 兩發太近，第 2 發應 sleep 並回報
        _, parsed, _ = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "10", "CLAUDE_HOT_LIMIT_MIN_GAP": "2"})
        self.assertFalse(is_deny(parsed))
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "10", "CLAUDE_HOT_LIMIT_MIN_GAP": "2"})
        self.assertIsNotNone(parsed, "第 2 發應有 systemMessage（min-gap sleep），stdout=%r" % raw)
        self.assertIn("systemMessage", parsed, "min-gap 應回報已間隔，stdout=%r" % raw)

    def test_malformed_stdin_fails_open(self):
        # fail-open：壞 stdin 不應癱瘓，靜默放行
        proc = subprocess.run(
            [sys.executable, HOOK], input="not json at all",
            capture_output=True, text=True,
            env={k: v for k, v in os.environ.items()
                 if not k.startswith("CLAUDE_HOT_LIMIT_") and k != "CLAUDE_PLUGIN_DATA"},
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


class WorkflowNudgeTest(unittest.TestCase):
    """Workflow 寬度提醒（heat-aware nudge）：近期撞過牆 + launch Workflow → 出聲提醒；
    冷（無/過期 trip）→ 安靜；Agent 不 nudge；env 可關。純提醒、絕不 deny。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "ledger")
        os.makedirs(self.data, exist_ok=True)
        # MAX 放寬、MIN_GAP=0 → 隔離出 nudge 這條路徑（不被 burst/​sleep 訊息干擾）
        self.env = {
            "CLAUDE_HOT_LIMIT_DATA": self.data,
            "CLAUDE_HOT_LIMIT_MAX": "999",
            "CLAUDE_HOT_LIMIT_MIN_GAP": "0",
            "CLAUDE_HOT_LIMIT_WINDOW": "600",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def seed_trip(self, error="rate_limit", age=5):
        now = time.time()
        with open(os.path.join(self.data, "trips-raw.jsonl"), "a") as f:
            f.write(json.dumps({"recorded_at": now - age,
                                "payload": {"error": error}}) + "\n")

    def fire(self, tool="Workflow", extra=None):
        return run_hook(tool=tool, env_overrides=dict(self.env, **(extra or {})))

    def test_nudges_when_recent_trip(self):
        self.seed_trip("rate_limit", age=5)
        _, parsed, raw = self.fire("Workflow")
        self.assertFalse(is_deny(parsed), "nudge 只提醒、絕不 deny，stdout=%r" % raw)
        self.assertIsNotNone(parsed, "近期撞牆 + Workflow 應出聲，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed, "應走 systemMessage，stdout=%r" % raw)
        self.assertIn("fan-out", parsed["systemMessage"],
                      "提醒應點名 fan-out 寬度，msg=%r" % parsed.get("systemMessage"))

    def test_silent_when_cold(self):
        # 沒有任何 trip → bucket 不燙 → 完全安靜（冷時不出聲是設計預設）
        _, parsed, raw = self.fire("Workflow")
        self.assertEqual(raw, "", "冷 bucket 不該 nudge，stdout=%r" % raw)

    def test_silent_when_trip_outside_window(self):
        # trip 在 window 外（很久以前）→ 視為已冷 → 安靜
        self.seed_trip("rate_limit", age=9999)
        _, parsed, raw = self.fire("Workflow")
        self.assertEqual(raw, "", "過期 trip 不算燙，stdout=%r" % raw)

    def test_benign_trip_does_not_count_as_heat(self):
        # 明確非 rate-limit（invalid_request）不算 bucket 燙 → 不 nudge
        self.seed_trip("invalid_request", age=5)
        _, parsed, raw = self.fire("Workflow")
        self.assertEqual(raw, "", "benign error 不算熱，stdout=%r" % raw)

    def test_agent_not_nudged(self):
        # 單一 Agent 是寬度 1、非病灶 → 即使 bucket 燙也不 nudge
        self.seed_trip("rate_limit", age=5)
        _, parsed, raw = self.fire("Agent")
        self.assertEqual(raw, "", "Agent 不該觸發 workflow 寬度提醒，stdout=%r" % raw)

    def test_nudge_disabled_by_env(self):
        self.seed_trip("rate_limit", age=5)
        _, parsed, raw = self.fire("Workflow", extra={"CLAUDE_HOT_LIMIT_WORKFLOW_NUDGE": "0"})
        self.assertEqual(raw, "", "NUDGE=0 應關閉提醒，stdout=%r" % raw)


class PerModelLedgerTest(unittest.TestCase):
    """v1.4.0：ledger 按 model 分桶記錄與計數。官方文檔證實 Opus / Sonnet 5 / Sonnet 4.x /
    Haiku 是各自獨立的 rate-limit 桶（Sonnet 5 明文獨立於 Sonnet 4.x 之外），共用同一個 burst
    計數器會誤報。model 從 transcript_path 尾端偵測最後一筆真實 assistant turn（即時反映
    /model 中途切換，不像 SessionStart 快照會過期）；effort 直接讀 payload 頂層欄位。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "ledger")
        os.makedirs(self.data, exist_ok=True)
        self.env = {"CLAUDE_HOT_LIMIT_DATA": self.data, "CLAUDE_HOT_LIMIT_MIN_GAP": "0"}

    def tearDown(self):
        self.tmp.cleanup()

    def fire_with_model(self, models, tool="Workflow", extra=None, effort=None):
        tp = make_transcript(self.tmp.name, models)
        payload = {"tool_name": tool, "tool_input": {}, "transcript_path": tp}
        if effort is not None:
            payload["effort"] = {"level": effort}
        return run_hook(tool=tool, env_overrides=dict(self.env, **(extra or {})), payload=payload)

    def test_ledger_row_records_detected_model(self):
        _, parsed, raw = self.fire_with_model(["claude-sonnet-5"], extra={"CLAUDE_HOT_LIMIT_MAX": "10"})
        self.assertFalse(is_deny(parsed), "stdout=%r" % raw)
        row = last_ledger_row(self.data)
        self.assertEqual(row.get("model"), "claude-sonnet-5", "row=%r" % row)

    def test_synthetic_entry_skipped_finds_real_model_before_it(self):
        # 最後一筆是 <synthetic>（compaction 摘要佔位）→ 要往前找到真正的 assistant model
        _, parsed, raw = self.fire_with_model(
            ["claude-opus-4-8", None], extra={"CLAUDE_HOT_LIMIT_MAX": "10"})
        self.assertFalse(is_deny(parsed), "stdout=%r" % raw)
        row = last_ledger_row(self.data)
        self.assertEqual(row.get("model"), "claude-opus-4-8", "row=%r" % row)

    def test_missing_transcript_records_unknown_and_fails_open(self):
        payload = {"tool_name": "Workflow", "tool_input": {},
                   "transcript_path": os.path.join(self.tmp.name, "does-not-exist.jsonl")}
        code, parsed, raw = run_hook(
            tool="Workflow", env_overrides=dict(self.env, CLAUDE_HOT_LIMIT_MAX="10"), payload=payload)
        self.assertEqual(code, 0, "transcript 讀不到要 fail-open，stdout=%r" % raw)
        self.assertFalse(is_deny(parsed))
        row = last_ledger_row(self.data)
        self.assertEqual(row.get("model"), "unknown", "row=%r" % row)

    def test_effort_captured_from_payload(self):
        _, parsed, raw = self.fire_with_model(
            ["claude-sonnet-5"], extra={"CLAUDE_HOT_LIMIT_MAX": "10"}, effort="xhigh")
        self.assertFalse(is_deny(parsed), "stdout=%r" % raw)
        row = last_ledger_row(self.data)
        self.assertEqual(row.get("effort"), "xhigh", "row=%r" % row)

    def test_effort_missing_defaults_unknown(self):
        self.fire_with_model(["claude-sonnet-5"], extra={"CLAUDE_HOT_LIMIT_MAX": "10"})
        row = last_ledger_row(self.data)
        self.assertEqual(row.get("effort"), "unknown", "row=%r" % row)

    def test_two_models_burst_independently(self):
        # MAX=2：Sonnet 5 連發 2 次吃滿上限，Opus 緊接著發不該被 Sonnet 5 的計數波及
        extra = {"CLAUDE_HOT_LIMIT_MAX": "2"}
        for _ in range(2):
            _, parsed, raw = self.fire_with_model(["claude-sonnet-5"], extra=extra)
            self.assertFalse(is_deny(parsed), "Sonnet 5 前 2 發不應被擋，stdout=%r" % raw)
        _, parsed, raw = self.fire_with_model(["claude-sonnet-5"], extra=extra)
        self.assertTrue(is_deny(parsed), "Sonnet 5 第 3 發應被擋，stdout=%r" % raw)

        for _ in range(2):
            _, parsed, raw = self.fire_with_model(["claude-opus-4-8"], extra=extra)
            self.assertFalse(is_deny(parsed), "Opus 不該被 Sonnet 5 的桶波及，stdout=%r" % raw)
        _, parsed, raw = self.fire_with_model(["claude-opus-4-8"], extra=extra)
        self.assertTrue(is_deny(parsed), "Opus 自己第 3 發也該被擋，stdout=%r" % raw)

    def test_legacy_rows_without_model_count_against_any_model(self):
        # 模擬升級前寫入的舊格式列（無 model key）——保守起見，一律計入任何 model 的窗口，
        # 避免改版後頭 WINDOW 秒漏算真實 burst。
        ledger = os.path.join(self.data, "launches.jsonl")
        now = time.time()
        with open(ledger, "w") as f:
            f.write(json.dumps({"ts": now - 5, "tool": "Workflow"}) + "\n")
            f.write(json.dumps({"ts": now - 3, "tool": "Workflow"}) + "\n")
        _, parsed, raw = self.fire_with_model(["claude-sonnet-5"], extra={"CLAUDE_HOT_LIMIT_MAX": "2"})
        self.assertTrue(is_deny(parsed), "舊格式列應保守計入任何 model，stdout=%r" % raw)

    def test_deny_message_names_the_model(self):
        extra = {"CLAUDE_HOT_LIMIT_MAX": "1"}
        self.fire_with_model(["claude-sonnet-5"], extra=extra)
        _, parsed, raw = self.fire_with_model(["claude-sonnet-5"], extra=extra)
        self.assertTrue(is_deny(parsed), "stdout=%r" % raw)
        reason = parsed["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("claude-sonnet-5", reason, "deny 訊息應點名燙的是哪個 model，reason=%r" % reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
