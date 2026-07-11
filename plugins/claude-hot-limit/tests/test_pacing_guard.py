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

    def test_corrupt_ledger_line_does_not_kill_guard(self):
        # Round-2 verify MEDIUM（DA，reproduced）：launches.jsonl 一行非 dict JSON（12345）
        # 讓 e.get("ts") 拋 AttributeError 逸出 main()（critical section 只有 finally 沒有
        # except）→ 每次 launch 都 exit 1 → deny/記帳/nudge 全死，且帳本 append-only =
        # **永久**失效直到手動修檔。這是本 PR 唯一漏掉加固的 JSONL reader。
        os.makedirs(self.data, exist_ok=True)
        with open(os.path.join(self.data, "launches.jsonl"), "w") as f:
            f.write("12345\n")  # 毒列
            # 3 筆近期有效 launch，MAX=1 應該要 deny
            now = __import__("time").time()
            for dt in (5, 10, 15):
                f.write(json.dumps({"ts": now - dt, "tool": "Workflow"}) + "\n")
        code, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "1"})
        self.assertEqual(code, 0, "毒列不該讓 guard crash，stdout=%r" % raw)
        self.assertTrue(is_deny(parsed),
                        "毒列只跳過，有效 entries 仍應觸發 burst deny，stdout=%r" % raw)

    def test_min_gap_sleeps_and_reports(self):
        # MAX 放寬避免 burst deny；MIN_GAP=2 → 兩發太近，第 2 發應 sleep 並回報
        _, parsed, _ = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "10", "CLAUDE_HOT_LIMIT_MIN_GAP": "2"})
        self.assertFalse(is_deny(parsed))
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "10", "CLAUDE_HOT_LIMIT_MIN_GAP": "2"})
        self.assertIsNotNone(parsed, "第 2 發應有 systemMessage（min-gap sleep），stdout=%r" % raw)
        self.assertIn("systemMessage", parsed, "min-gap 應回報已間隔，stdout=%r" % raw)

    def test_non_dict_json_stdin_fails_open(self):
        # Re-verify finding 8（DA 重現）：合法 JSON 但非 dict 的 stdin（[1,2,3]）
        # 舊版在 payload.get 拋 AttributeError（exit 1）。叢集 B 的威脅模型兩個 hook 都要防。
        proc = subprocess.run(
            [sys.executable, HOOK], input="[1, 2, 3]",
            capture_output=True, text=True,
            env={k: v for k, v in os.environ.items()
                 if not k.startswith("CLAUDE_HOT_LIMIT_") and k != "CLAUDE_PLUGIN_DATA"},
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, "非 dict payload 應 fail-open，stderr=%r" % proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", "應靜默放行")

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


class RateStateHeatNudgeTest(unittest.TestCase):
    """add-rate-limit-proxy task 3.2：heat-aware nudge 優先信任 rate-limit-proxy 落地的
    rate-state.jsonl（若存在且有 WINDOW 內近期快照）判斷真實 bucket 熱度，取代（不是疊加）
    trips-raw.jsonl 啟發式；該檔案不存在 / 太舊 / 解析失敗 → fail-open fallback 回既有邏輯。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "ledger")
        os.makedirs(self.data, exist_ok=True)
        self.env = {
            "CLAUDE_HOT_LIMIT_DATA": self.data,
            "CLAUDE_HOT_LIMIT_MAX": "999",
            "CLAUDE_HOT_LIMIT_MIN_GAP": "0",
            "CLAUDE_HOT_LIMIT_WINDOW": "600",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def fire(self, tool="Workflow", extra=None):
        return run_hook(tool=tool, env_overrides=dict(self.env, **(extra or {})))

    def seed_rate_state(self, age=5, **fields):
        row = {
            "ts": time.time() - age,
            "rl_requests_remaining": None, "rl_requests_reset": None,
            "rl_input_tokens_remaining": None, "rl_input_tokens_reset": None,
            "rl_output_tokens_remaining": None, "rl_output_tokens_reset": None,
            "usage": None,
        }
        row.update(fields)
        with open(os.path.join(self.data, "rate-state.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")

    def seed_hot_trip(self, age=5):
        with open(os.path.join(self.data, "trips-raw.jsonl"), "a") as f:
            f.write(json.dumps({"recorded_at": time.time() - age,
                                "payload": {"error": "rate_limit"}}) + "\n")

    def test_nudges_when_rate_state_shows_low_requests_remaining(self):
        self.seed_rate_state(age=5, rl_requests_remaining=2)
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_RATE_STATE_MIN_REQUESTS": "10"})
        self.assertFalse(is_deny(parsed), "nudge 只提醒、絕不 deny，stdout=%r" % raw)
        self.assertIsNotNone(parsed, "真實資料顯示 requests 偏低應出聲，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed, "應走 systemMessage，stdout=%r" % raw)
        self.assertIn("fan-out", parsed["systemMessage"],
                      "提醒仍應點名 fan-out 寬度，msg=%r" % parsed.get("systemMessage"))
        self.assertIn("requests remaining", parsed["systemMessage"],
                      "應點名是 requests 桶偏低，msg=%r" % parsed.get("systemMessage"))

    def test_nudges_when_rate_state_shows_low_token_remaining(self):
        # 涵蓋三個欄位的「或」語意：input tokens 偏低也該觸發，不只 requests
        self.seed_rate_state(age=5, rl_requests_remaining=500, rl_input_tokens_remaining=100)
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_RATE_STATE_MIN_REQUESTS": "10",
                                          "CLAUDE_HOT_LIMIT_RATE_STATE_MIN_TOKENS": "2000"})
        self.assertIsNotNone(parsed, "真實資料顯示 input tokens 偏低應出聲，stdout=%r" % raw)
        self.assertIn("input tokens remaining", parsed["systemMessage"],
                      "應點名是 input tokens 桶偏低，msg=%r" % parsed.get("systemMessage"))

    def test_healthy_rate_state_suppresses_nudge_even_if_trips_raw_hot(self):
        # 核心：rate-state.jsonl 存在時「取代」trips-raw 啟發式，不是「疊加」——即使舊邏輯會
        # 因為近期 trip 判熱，真實資料顯示 budget 充足時，仍應保持安靜。
        self.seed_hot_trip(age=5)
        self.seed_rate_state(age=5, rl_requests_remaining=500,
                              rl_input_tokens_remaining=50000, rl_output_tokens_remaining=50000)
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_RATE_STATE_MIN_REQUESTS": "10",
                                          "CLAUDE_HOT_LIMIT_RATE_STATE_MIN_TOKENS": "2000"})
        self.assertEqual(raw, "",
                          "真實資料確認 budget 充足時應保持安靜，即使 trips-raw 有近期撞牆，stdout=%r" % raw)

    def test_falls_back_to_trips_raw_when_rate_state_file_absent(self):
        # 沒有 rate-state.jsonl → 應沿用既有 trips-raw.jsonl 邏輯（回歸安全網，行為不可變）
        self.seed_hot_trip(age=5)
        _, parsed, raw = self.fire()
        self.assertIsNotNone(parsed, "無 rate-state.jsonl 時應 fallback 到 trips-raw 邏輯，stdout=%r" % raw)
        self.assertIn("撞牆", parsed["systemMessage"],
                      "fallback 應該是既有 trips-raw 訊息措辭，msg=%r" % parsed.get("systemMessage"))

    def test_falls_back_when_rate_state_file_corrupt(self):
        # rate-state.jsonl 存在但整份都壞掉 → fail-open：不能讓 hook 掛掉或誤判，安靜 fallback
        with open(os.path.join(self.data, "rate-state.jsonl"), "w") as f:
            f.write("{not valid json\n")
            f.write("also not valid\n")
        self.seed_hot_trip(age=5)
        _, parsed, raw = self.fire()
        self.assertIsNotNone(parsed, "rate-state.jsonl 全毀應安靜 fallback 到 trips-raw，stdout=%r" % raw)
        self.assertIn("撞牆", parsed["systemMessage"], "msg=%r" % parsed.get("systemMessage"))

    def test_falls_back_when_rate_state_record_stale(self):
        # 最後一筆 rate-state 遠超過 window → token bucket 可能早已回填，陳舊快照不可信 → fallback
        self.seed_rate_state(age=9999, rl_requests_remaining=1)
        self.seed_hot_trip(age=5)
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_RATE_STATE_MIN_REQUESTS": "10"})
        self.assertIsNotNone(parsed, "陳舊 rate-state 快照不可信，應 fallback 到 trips-raw，stdout=%r" % raw)
        self.assertIn("撞牆", parsed["systemMessage"], "msg=%r" % parsed.get("systemMessage"))

    def test_agent_not_nudged_even_with_hot_rate_state(self):
        # 單一 Agent 是寬度 1、非病灶 → 即使 rate-state 顯示真實 budget 偏低也不 nudge
        self.seed_rate_state(age=5, rl_requests_remaining=1)
        _, parsed, raw = self.fire(tool="Agent", extra={"CLAUDE_HOT_LIMIT_RATE_STATE_MIN_REQUESTS": "10"})
        self.assertEqual(raw, "", "Agent 不該觸發 workflow 寬度提醒，stdout=%r" % raw)

    def fire_as(self, model, tool="Workflow", extra=None):
        tp = make_transcript(self.tmp.name, [model])
        payload = {"tool_name": tool, "tool_input": {}, "transcript_path": tp}
        return run_hook(tool=tool, env_overrides=dict(self.env, **(extra or {})), payload=payload)

    def test_rate_state_heat_scoped_to_current_bucket(self):
        # #4/D4：只有 opus 桶的低 remaining 記錄，當前是 sonnet-5 → 跨桶，不該 nudge。
        # 無 bucket 過濾的實作會用最後一筆（opus）判熱 → 誤 nudge，故此測試 RED。
        self.seed_rate_state(age=5, model="claude-opus-4-8", rl_requests_remaining=1)
        _, parsed, raw = self.fire_as("claude-sonnet-5",
                                      extra={"CLAUDE_HOT_LIMIT_RATE_STATE_MIN_REQUESTS": "10"})
        self.assertEqual(raw, "", "opus 桶的 rate-state 不該讓 sonnet-5 的 nudge 誤判，stdout=%r" % raw)

    def test_rate_state_same_bucket_nudges(self):
        # 同桶（sonnet-4-5 記錄、當前 sonnet-4-6）→ 應 nudge。
        self.seed_rate_state(age=5, model="claude-sonnet-4-5", rl_requests_remaining=1)
        _, parsed, raw = self.fire_as("claude-sonnet-4-6",
                                      extra={"CLAUDE_HOT_LIMIT_RATE_STATE_MIN_REQUESTS": "10"})
        self.assertIsNotNone(parsed, "同 sonnet-4 桶的 rate-state 偏低應 nudge，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed)

    def test_legacy_rate_state_without_model_counts_as_unscoped(self):
        # 舊格式 rate-state 記錄（無 model 欄，proxy 加 model 前寫的）→ unscoped，計入任何桶。
        self.seed_rate_state(age=5, rl_requests_remaining=1)  # 無 model 欄
        _, parsed, raw = self.fire_as("claude-sonnet-5",
                                      extra={"CLAUDE_HOT_LIMIT_RATE_STATE_MIN_REQUESTS": "10"})
        self.assertIsNotNone(parsed, "無 model 的舊 rate-state 記錄應 unscoped 計入，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed)


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

    def test_u2028_in_transcript_line_is_not_a_record_boundary(self):
        # Round-2 verify LOW（security）：transcript 是 newline-delimited JSONL，一筆記錄 =
        # 一條物理行。str.splitlines() 卻會額外在 U+2028/U+2029 等 Unicode line separator
        # 斷行——這些字元 JSON 規範允許不 escape 地出現在字串「內容」裡。後果：某行內容嵌
        # 的 {"model":...} 片段被當成獨立記錄、且因反向掃描先命中 → 冒充真實 model。改用
        # split("\n") 只以換行為記錄邊界，消除此面（兩份 detect_model 副本同步）。
        u2028 = "\u2028"
        fake = '{"type": "assistant", "message": {"model": "spoofed-evil"}}'
        # 一條物理行（無換行）：真實 assistant 物件 + 字面 U+2028 + 嵌入的偽造片段。
        # splitlines() 會把它切成兩筆、反向先取 fake；split("\n") 視為單行 → 解析失敗跳過，
        # 退回前一行的真實 model。兩種行為在此可區分。
        poison_line = '{"type": "assistant", "message": {"model": "claude-sonnet-5"}}' + u2028 + fake
        tp = os.path.join(self.tmp.name, "u2028.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5"}}) + "\n")
            f.write(poison_line + "\n")  # 最後一條物理行
        payload = {"tool_name": "Workflow", "tool_input": {}, "transcript_path": tp}
        run_hook(tool="Workflow", env_overrides=dict(self.env, CLAUDE_HOT_LIMIT_MAX="10"), payload=payload)
        row = last_ledger_row(self.data)
        self.assertNotEqual(row.get("model"), "spoofed-evil",
                            "U+2028 內嵌片段不該被當成獨立記錄冒充 model，row=%r" % row)
        self.assertEqual(row.get("model"), "claude-sonnet-5", "應取真實 model，row=%r" % row)

    def test_adversarial_transcript_does_not_crash_guard(self):
        # Re-verify finding 7：guard 側 detect_model 的非 dict 防禦（叢集 B 同步修）此前零測試
        # 覆蓋——副本漂移會讓整個 guard 靜默失效（crash = exit 1 = 非阻擋 = 不 deny 也不記帳）。
        # 本測試把防禦釘住：壞行（bare 數字、非 dict message）混在真實行中，guard 照常運作。
        tp = os.path.join(self.tmp.name, "adversarial.jsonl")
        with open(tp, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5"}}) + "\n")
            f.write("12345\n")  # 合法 JSON、非 dict
            f.write(json.dumps({"type": "assistant", "message": "plain string"}) + "\n")
        payload = {"tool_name": "Workflow", "tool_input": {}, "transcript_path": tp}
        code, parsed, raw = run_hook(
            tool="Workflow", env_overrides=dict(self.env, CLAUDE_HOT_LIMIT_MAX="10"), payload=payload)
        self.assertEqual(code, 0, "adversarial transcript 不該讓 guard crash，stdout=%r" % raw)
        self.assertFalse(is_deny(parsed))
        row = last_ledger_row(self.data)
        self.assertEqual(row.get("model"), "claude-sonnet-5",
                          "應跳過壞行、往前找到真實 model，row=%r" % row)

    def test_fifo_transcript_does_not_block_guard(self):
        # Round-2 verify INFO（finding 14）：guard 側 detect_model 的 FIFO 防禦（6370e48
        # 兩副本同步修）此前只在 trip-recorder 側有測試——補上 guard 側的覆蓋釘。
        fifo = os.path.join(self.tmp.name, "transcript.fifo")
        os.mkfifo(fifo)
        payload = {"tool_name": "Workflow", "tool_input": {}, "transcript_path": fifo}
        code, parsed, raw = run_hook(
            tool="Workflow", env_overrides=dict(self.env, CLAUDE_HOT_LIMIT_MAX="10"), payload=payload)
        self.assertEqual(code, 0, "FIFO transcript 不該 block guard")
        self.assertFalse(is_deny(parsed))
        row = last_ledger_row(self.data)
        self.assertEqual(row.get("model"), "unknown", "非一般檔案 → fail-open 記 unknown")

    def _seed_launches(self, model, n, age=5):
        now = time.time()
        with open(os.path.join(self.data, "launches.jsonl"), "a") as f:
            for _ in range(n):
                f.write(json.dumps({"ts": now - age, "tool": "Workflow", "model": model}) + "\n")

    def test_same_bucket_variants_share_burst_window(self):
        # #6：claude-sonnet-4-5 與 4-6 同屬 sonnet-4 桶。seed 2 筆 4-5、MAX=2，fire 4-6 →
        # 應 DENY（bucket 合併計數）。exact-id 實作 count=0（4-5≠4-6）→ 放行，故此測試 RED。
        self._seed_launches("claude-sonnet-4-5", 2)
        _, parsed, raw = self.fire_with_model(["claude-sonnet-4-6"], extra={"CLAUDE_HOT_LIMIT_MAX": "2"})
        self.assertTrue(is_deny(parsed), "同 sonnet-4 桶的變體應合併觸發 burst deny，stdout=%r" % raw)

    def test_different_bucket_does_not_share_burst_window(self):
        # 反向防 over-merge：sonnet-5 是獨立桶，不該被 sonnet-4 的 launch 擋。
        self._seed_launches("claude-sonnet-4-5", 2)
        _, parsed, raw = self.fire_with_model(["claude-sonnet-5"], extra={"CLAUDE_HOT_LIMIT_MAX": "2"})
        self.assertFalse(is_deny(parsed), "sonnet-5 是獨立桶，不該被 sonnet-4 launch 擋，stdout=%r" % raw)

    def test_legacy_ledger_row_without_model_counts_any_bucket(self):
        # 舊格式列（無 model key）保守計入任何 bucket——bucket 化不得破壞此既有語意。
        now = time.time()
        with open(os.path.join(self.data, "launches.jsonl"), "a") as f:
            for _ in range(2):
                f.write(json.dumps({"ts": now - 5, "tool": "Workflow"}) + "\n")  # 無 model
        _, parsed, raw = self.fire_with_model(["claude-sonnet-4-6"], extra={"CLAUDE_HOT_LIMIT_MAX": "2"})
        self.assertTrue(is_deny(parsed), "無 model 舊列應保守計入任何 bucket，stdout=%r" % raw)

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


class PerModelHeatNudgeTest(unittest.TestCase):
    """#2 — recent_heat() 按 model 分桶：Sonnet 5 撞牆的紀錄不該讓 Opus 的 nudge 誤判為熱。

    篩選慣例比照 v1.4.0 launches.jsonl：trip 缺 model 欄位（舊格式）→ 保守計入任何 model；
    有值 → 需與當前 launch 的 model 相符才計入。無 rate-state.jsonl → fallback 到 recent_heat。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "ledger")
        os.makedirs(self.data, exist_ok=True)
        self.env = {
            "CLAUDE_HOT_LIMIT_DATA": self.data,
            "CLAUDE_HOT_LIMIT_MAX": "999",
            "CLAUDE_HOT_LIMIT_MIN_GAP": "0",
            "CLAUDE_HOT_LIMIT_WINDOW": "600",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def seed_trip(self, model=None, age=5):
        """model=None → 舊格式列（無 model 欄位）。"""
        row = {"recorded_at": time.time() - age, "payload": {"error": "rate_limit"}}
        if model is not None:
            row["model"] = model
        with open(os.path.join(self.data, "trips-raw.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")

    def fire_workflow_as(self, model):
        tp = make_transcript(self.tmp.name, [model])
        payload = {"tool_name": "Workflow", "tool_input": {}, "transcript_path": tp}
        return run_hook(tool="Workflow", env_overrides=self.env, payload=payload)

    def test_different_model_trip_does_not_nudge(self):
        self.seed_trip(model="claude-sonnet-5", age=5)
        _, parsed, raw = self.fire_workflow_as("claude-opus-4-8")
        self.assertEqual(raw, "", "Sonnet 5 的 trip 不該讓 Opus 的 nudge 誤判為熱，stdout=%r" % raw)

    def test_same_model_trip_nudges(self):
        self.seed_trip(model="claude-opus-4-8", age=5)
        _, parsed, raw = self.fire_workflow_as("claude-opus-4-8")
        self.assertIsNotNone(parsed, "同 model 的近期 trip 應觸發 nudge，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed)

    def test_legacy_trip_without_model_counts_any(self):
        self.seed_trip(model=None, age=5)  # 舊格式列
        _, parsed, raw = self.fire_workflow_as("claude-opus-4-8")
        self.assertIsNotNone(parsed, "舊格式列應保守計入任何 model，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed)

    def test_non_dict_payload_row_does_not_silence_nudge(self):
        # Round-2 verify MEDIUM（logic，live repro）：trip-recorder 叢集 B 修復會合法寫出
        # payload 為非 dict 的列（{"recorded_at":…,"model":"unknown","payload":[1,2,3]}）——
        # recent_heat 的 p.get("error") 對非 dict payload 拋 AttributeError 被外層 except
        # 吞掉 → 所有 nudge 靜默。讀寫兩側必須一致：這種列要保守視為熱（寧記勿漏）。
        with open(os.path.join(self.data, "trips-raw.jsonl"), "a") as f:
            f.write(json.dumps({"recorded_at": time.time() - 5, "model": "unknown",
                                "payload": [1, 2, 3]}) + "\n")
        self.seed_trip(model="claude-opus-4-8", age=5)
        _, parsed, raw = self.fire_workflow_as("claude-opus-4-8")
        self.assertIsNotNone(parsed, "非 dict payload 列不該靜默全部 nudge，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed)

    def test_bad_recorded_at_row_does_not_silence_nudge(self):
        # 同 MEDIUM 的姊妹路徑（DA 指出為永久變體）：recorded_at 是非數字字串 →
        # float() ValueError → 外層 except → 全滅。應只跳過該列。
        with open(os.path.join(self.data, "trips-raw.jsonl"), "a") as f:
            f.write(json.dumps({"recorded_at": "not-a-number",
                                "payload": {"error": "rate_limit"}}) + "\n")
        self.seed_trip(model="claude-opus-4-8", age=5)
        _, parsed, raw = self.fire_workflow_as("claude-opus-4-8")
        self.assertIsNotNone(parsed, "壞 recorded_at 列不該靜默全部 nudge，stdout=%r" % raw)

    def test_corrupt_line_in_trips_does_not_silence_nudge(self):
        # Re-verify finding 9：trips-raw.jsonl 內一行合法但非 dict 的 JSON（如 12345）
        # 舊版在 o.get() 拋 AttributeError 被外層 except 吞掉 → return None → 所有 nudge 靜默。
        # 正確行為：跳過壞行，其餘近期 trip 照常觸發 nudge。
        with open(os.path.join(self.data, "trips-raw.jsonl"), "a") as f:
            f.write("12345\n")  # 壞行（合法 JSON、非 dict）
        self.seed_trip(model="claude-opus-4-8", age=5)
        _, parsed, raw = self.fire_workflow_as("claude-opus-4-8")
        self.assertIsNotNone(parsed, "一行壞資料不該靜默全部 nudge，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed)

    def test_launch_side_unknown_still_nudges(self):
        # Verify 叢集 A（DA repro）：launch 當下 detect_model 失敗（無 transcript_path）
        # → model="unknown" 不可以把真實 model 的 trip 全部過濾掉——v1.5.0 會出提醒，修復後要恢復。
        self.seed_trip(model="claude-sonnet-5", age=5)
        _, parsed, raw = run_hook(tool="Workflow", env_overrides=self.env)  # payload 無 transcript_path
        self.assertIsNotNone(parsed, "launch 側偵測失敗不該靜默所有 nudge（fail-open），stdout=%r" % raw)
        self.assertIn("systemMessage", parsed)

    def test_record_side_unknown_still_nudges(self):
        # Verify 叢集 A：trip 記成 model="unknown"（StopFailure 當下 transcript 讀不到）
        # → 對任何真實 model 的 launch 都應保守計入（ambiguous trip ≠ 別人的桶）。
        self.seed_trip(model="unknown", age=5)
        _, parsed, raw = self.fire_workflow_as("claude-opus-4-8")
        self.assertIsNotNone(parsed, "record 側 unknown trip 應保守計入任何 model，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed)

    def test_same_bucket_variant_trip_nudges(self):
        # #6：sonnet-4-5 撞牆的 trip 應讓 sonnet-4-6 的 nudge 觸發（同 sonnet-4 桶）。
        # exact-id 實作排除（4-5≠4-6）→ 無 nudge，故此測試 RED。
        self.seed_trip(model="claude-sonnet-4-5", age=5)
        _, parsed, raw = self.fire_workflow_as("claude-sonnet-4-6")
        self.assertIsNotNone(parsed, "同 sonnet-4 桶的 trip 應觸發 nudge，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed)

    def test_different_bucket_variant_trip_does_not_nudge(self):
        # 反向防 over-merge：sonnet-4-5 的 trip 不該讓 sonnet-5（獨立桶）誤判為熱。
        self.seed_trip(model="claude-sonnet-4-5", age=5)
        _, parsed, raw = self.fire_workflow_as("claude-sonnet-5")
        self.assertEqual(raw, "", "sonnet-4 的 trip 不該讓 sonnet-5 誤判為熱，stdout=%r" % raw)


class FileOverrideTest(unittest.TestCase):
    """#3 — MAX/MIN_GAP 支援檔案旗標即時切換（env var 不 hot-reload，檔案每次執行重新讀）。

    優先序：<data_dir>/max-override（min-gap-override）→ env var → code default。
    檔案不存在 / 內容無法解析 → fail-open fallback env var，不 crash。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "ledger")
        os.makedirs(self.data, exist_ok=True)
        self.env = {"CLAUDE_HOT_LIMIT_DATA": self.data, "CLAUDE_HOT_LIMIT_MIN_GAP": "0"}

    def tearDown(self):
        self.tmp.cleanup()

    def write_override(self, filename, content):
        with open(os.path.join(self.data, filename), "w") as f:
            f.write(content)

    def fire(self, extra=None):
        return run_hook(tool="Workflow", env_overrides=dict(self.env, **(extra or {})))

    def test_max_override_file_takes_precedence_over_env(self):
        # env 說 999（觀測模式），檔案說 1 → 檔案贏：第 2 發就該被擋
        self.write_override("max-override", "1")
        _, parsed, _ = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "999"})
        self.assertFalse(is_deny(parsed), "第 1 發應放行")
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "999"})
        self.assertTrue(is_deny(parsed),
                        "max-override=1 應蓋過 env MAX=999，第 2 發被擋，stdout=%r" % raw)

    def test_missing_override_falls_back_to_env(self):
        # 無檔案 → env MAX=2 生效：第 3 發被擋（既有行為不變）
        for _ in range(2):
            _, parsed, _ = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "2"})
            self.assertFalse(is_deny(parsed))
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "2"})
        self.assertTrue(is_deny(parsed), "無 override 檔案應 fallback env，stdout=%r" % raw)

    def test_unparseable_override_falls_back_to_env(self):
        # 檔案內容不是數字 → fail-open fallback env MAX=1，不 crash
        self.write_override("max-override", "not a number\n")
        code, parsed, _ = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "1"})
        self.assertEqual(code, 0, "壞內容不該讓 hook crash")
        self.assertFalse(is_deny(parsed), "第 1 發應放行（env MAX=1 生效）")
        code, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "1"})
        self.assertEqual(code, 0)
        self.assertTrue(is_deny(parsed), "壞內容應 fallback env MAX=1：第 2 發被擋，stdout=%r" % raw)

    def test_max_override_zero_denies_without_crash(self):
        # Verify 叢集 C（logic lens exact repro）：max-override=0 + 空帳本 →
        # count(0) >= max(0) 進 deny 分支，舊版對空 entries 取 entries[0] 拋 IndexError（exit 1）。
        # 「0 = 全擋」是合理的使用者意圖 → 應給正常 deny，不是 crash。
        self.write_override("max-override", "0")
        code, parsed, raw = self.fire()
        self.assertEqual(code, 0, "max-override=0 不該讓 hook crash，stdout=%r" % raw)
        self.assertTrue(is_deny(parsed), "0 = 全擋，第 1 發就該被正常 deny，stdout=%r" % raw)

    def test_max_override_negative_denies_without_crash(self):
        self.write_override("max-override", "-5")
        code, parsed, raw = self.fire()
        self.assertEqual(code, 0, "負值不該 crash，stdout=%r" % raw)
        self.assertTrue(is_deny(parsed), "負值視同全擋，stdout=%r" % raw)

    def test_fifo_override_without_disabled_does_not_block(self):
        # Round-2 verify LOW（logic，reproduced >8s block）：FIFO max-override 且**沒有**
        # disabled 旗標時，每次 launch 都會卡到 hook timeout——finding-3 的 isfile 原則
        # 必須同樣套用到 override 檔這個新讀取點。
        os.mkfifo(os.path.join(self.data, "max-override"))
        code, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "10"})
        self.assertEqual(code, 0, "FIFO override 不該 block（timeout=30 會抓到）")
        self.assertFalse(is_deny(parsed), "應 fallback env MAX=10 正常放行，stdout=%r" % raw)

    def test_disabled_flag_rescues_before_override_reads(self):
        # Re-verify finding 4：disabled kill-switch 必須在 override 檔讀取「之前」檢查——
        # 若 max-override 是 FIFO（無 writer 的 open 會永久 block），disabled 旗標應仍能救援。
        os.mkfifo(os.path.join(self.data, "max-override"))
        open(os.path.join(self.data, "disabled"), "w").close()
        code, parsed, raw = self.fire()  # run_hook timeout=30：舊版會 block 到 timeout
        self.assertEqual(code, 0, "disabled 應在 override 讀取前生效，stdout=%r" % raw)
        self.assertEqual(raw, "", "disabled 生效 = 靜默放行")

    def test_unparseable_override_warns_on_stderr(self):
        # Re-verify finding 10：壞內容靜默 fallback 會讓使用者以為保護已開。應在 stderr 警告。
        self.write_override("max-override", "not a number\n")
        proc = subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"tool_name": "Workflow", "tool_input": {}}),
            capture_output=True, text=True,
            env={**{k: v for k, v in os.environ.items()
                    if not k.startswith("CLAUDE_HOT_LIMIT_") and k != "CLAUDE_PLUGIN_DATA"},
                 "CLAUDE_HOT_LIMIT_DATA": self.data, "CLAUDE_HOT_LIMIT_MIN_GAP": "0"},
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("max-override", proc.stderr,
                      "壞內容 fallback 應在 stderr 警告點名檔案，stderr=%r" % proc.stderr)

    def test_max_zero_deny_message_says_freeze_not_wait(self):
        # Re-verify finding 11：MAX ≤ 0 時 deny 訊息不該建議「等 ~600s」（等再久也沒用），
        # 應說明這是全面凍結、指向移除/調高 override。
        self.write_override("max-override", "0")
        _, parsed, raw = self.fire()
        self.assertTrue(is_deny(parsed), "stdout=%r" % raw)
        context = parsed["hookSpecificOutput"]["additionalContext"]
        self.assertIn("凍結", context, "MAX≤0 應說明全面凍結，context=%r" % context)
        self.assertNotIn("等約", context, "不該給無效的等待建議，context=%r" % context)

    def test_min_gap_override_file_works(self):
        # min-gap-override=60 蓋過 env MIN_GAP=0 → 距上一發太近應 sleep 並回報 systemMessage。
        # 確定性化（re-verify findings 6/15）：直接 seed 帳本一筆剛剛的 launch，不靠兩次
        # subprocess 的 wall-clock 間隔 < 2s（慢 CI 會 flaky）；SLEEP_CAP=1 讓測試只睡 1 秒。
        self.write_override("min-gap-override", "60")
        with open(os.path.join(self.data, "launches.jsonl"), "w") as f:
            f.write(json.dumps({"ts": time.time() - 0.1, "tool": "Workflow"}) + "\n")
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_MAX": "10",
                                           "CLAUDE_HOT_LIMIT_SLEEP_CAP": "1"})
        self.assertFalse(is_deny(parsed))
        self.assertIsNotNone(parsed, "min-gap-override 生效時應有 sleep 回報，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed, "應回報已間隔，stdout=%r" % raw)


class FableWorkflowGateTest(unittest.TestCase):
    """#18 — Fable 5 session 開 Workflow → deny（預設）/ warn / off。

    機制：Workflow fan-out 的 unpinned agent 繼承 session model；fable5（頂階/貴 model）
    × N 個並發 = 瞬間 token/session-limit 炸（idd-verify #205 失效模式）。gate 在 model
    偵測後、burst critical section 前，無條件於 burst/heat。MAX 設高 → 確保測到 fable-gate
    而非 burst-deny。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.account_dir = os.path.join(self.tmp.name, "account")
        self.base = {
            "CLAUDE_HOT_LIMIT_DATA": self.account_dir,
            "CLAUDE_HOT_LIMIT_MAX": "999",   # 高 → 不讓 burst-deny 干擾 fable-gate 測試
            "CLAUDE_HOT_LIMIT_MIN_GAP": "0",  # 不卡 sleep
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _payload(self, tool, models):
        tp = make_transcript(self.tmp.name, models)
        return {"tool_name": tool, "tool_input": {}, "transcript_path": tp}

    def test_fable_workflow_denied_by_default(self):
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["claude-fable-5"]),
                                  env_overrides=self.base)
        self.assertTrue(is_deny(parsed), "fable5 + Workflow 預設應 deny，stdout=%r" % raw)

    def test_fable_workflow_warn_mode(self):
        env = dict(self.base, CLAUDE_HOT_LIMIT_FABLE_WORKFLOW="warn")
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["claude-fable-5"]),
                                  env_overrides=env)
        self.assertFalse(is_deny(parsed), "warn 模式不應 deny，stdout=%r" % raw)
        self.assertIsNotNone(parsed, "warn 應有 systemMessage，stdout=%r" % raw)
        self.assertIn("systemMessage", parsed, "warn 應走 systemMessage，stdout=%r" % raw)

    def test_fable_workflow_off_mode(self):
        env = dict(self.base, CLAUDE_HOT_LIMIT_FABLE_WORKFLOW="off")
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["claude-fable-5"]),
                                  env_overrides=env)
        self.assertFalse(is_deny(parsed), "off 模式應放行（不 deny），stdout=%r" % raw)

    def test_fable_workflow_typo_fails_safe_deny(self):
        env = dict(self.base, CLAUDE_HOT_LIMIT_FABLE_WORKFLOW="deney")  # typo
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["claude-fable-5"]),
                                  env_overrides=env)
        self.assertTrue(is_deny(parsed), "不認得的值應 fail-safe deny（不 crash），stdout=%r" % raw)

    def test_non_fable_workflow_not_blocked(self):
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["claude-opus-4-8"]),
                                  env_overrides=self.base)
        self.assertFalse(is_deny(parsed), "非-fable + Workflow 不應被 fable-gate 擋，stdout=%r" % raw)

    def test_fable_agent_not_blocked(self):
        _, parsed, raw = run_hook(payload=self._payload("Agent", ["claude-fable-5"]),
                                  env_overrides=self.base)
        self.assertFalse(is_deny(parsed), "fable5 + Agent 不 fan-out、不應被擋，stdout=%r" % raw)

    def test_model_unknown_workflow_not_blocked(self):
        # 只有 <synthetic> turn → detect_model 回 unknown → fail-open 不擋
        _, parsed, raw = run_hook(payload=self._payload("Workflow", [None]),
                                  env_overrides=self.base)
        self.assertFalse(is_deny(parsed), "model unknown 時應 fail-open 不擋，stdout=%r" % raw)

    def test_global_off_switch_bypasses_fable_gate(self):
        env = dict(self.base, CLAUDE_HOT_LIMIT_OFF="1")
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["claude-fable-5"]),
                                  env_overrides=env)
        self.assertFalse(is_deny(parsed),
                         "CLAUDE_HOT_LIMIT_OFF=1 應先攔、天然 bypass fable-gate，stdout=%r" % raw)

    # --- verify-driven fixes（6-AI ensemble FAIL；#18 F1/F2/F3/F4）---

    def test_warn_mode_records_ledger(self):
        # F1：warn 應 fall-through 而非早退——launch 要記進 ledger（否則後續 burst 窗口低估）
        env = dict(self.base, CLAUDE_HOT_LIMIT_FABLE_WORKFLOW="warn")
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["claude-fable-5"]), env_overrides=env)
        self.assertFalse(is_deny(parsed))
        ledger = os.path.join(self.account_dir, "launches.jsonl")
        self.assertTrue(os.path.isfile(ledger), "warn 模式應 fall-through 記 ledger，stdout=%r" % raw)
        row = last_ledger_row(self.account_dir)
        self.assertEqual((row or {}).get("tool"), "Workflow", "ledger 應記這發 Workflow launch")

    def test_warn_mode_still_gets_fanout_advisory(self):
        # F1：warn 的寬 fable Workflow 應仍收到 #19 fan-out advisory（早退版本吞掉了它）
        env = dict(self.base, CLAUDE_HOT_LIMIT_FABLE_WORKFLOW="warn")
        tp = make_transcript(self.tmp.name, ["claude-fable-5"])
        payload = {"tool_name": "Workflow",
                   "tool_input": {"script": "const r = await parallel(items.map(x=>()=>agent('go')))"},
                   "transcript_path": tp}
        _, parsed, raw = run_hook(payload=payload, env_overrides=env)
        msg = (parsed or {}).get("systemMessage", "")
        self.assertIn("靜態估寬 fan-out", msg,
                      "warn 模式的寬 Workflow 應仍收到 #19 fan-out advisory，stdout=%r" % raw)

    def test_file_override_off_takes_effect(self):
        # F2：<data_dir>/fable-workflow 檔案 override（mid-session 生效，不像 env 需重開）
        os.makedirs(self.account_dir, exist_ok=True)
        with open(os.path.join(self.account_dir, "fable-workflow"), "w") as f:
            f.write("off")
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["claude-fable-5"]), env_overrides=self.base)
        self.assertFalse(is_deny(parsed), "fable-workflow 檔=off 應放行（file override），stdout=%r" % raw)

    def test_file_override_beats_env(self):
        # F2：檔案 override 優先於 env（比照 file_override_int 契約）
        os.makedirs(self.account_dir, exist_ok=True)
        with open(os.path.join(self.account_dir, "fable-workflow"), "w") as f:
            f.write("off")
        env = dict(self.base, CLAUDE_HOT_LIMIT_FABLE_WORKFLOW="deny")  # env 說 deny、檔案說 off
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["claude-fable-5"]), env_overrides=env)
        self.assertFalse(is_deny(parsed), "檔案 override 應優先於 env，stdout=%r" % raw)

    def test_typo_deny_message_names_the_value(self):
        # F3：fail-safe deny 訊息要點名打錯的值，讓使用者分辨「typo」vs「預設 deny」
        env = dict(self.base, CLAUDE_HOT_LIMIT_FABLE_WORKFLOW="deney")  # typo
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["claude-fable-5"]), env_overrides=env)
        self.assertTrue(is_deny(parsed))
        reason = (parsed or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
        self.assertIn("deney", reason, "typo 的 override 值應出現在 deny 訊息裡以區分預設，stdout=%r" % raw)

    def test_uppercase_fable_model_still_gated(self):
        # F4：is_fable 應 lowercase 再比對（大小寫變異不該讓安全 gate fail-open）
        _, parsed, raw = run_hook(payload=self._payload("Workflow", ["Claude-Fable-5"]), env_overrides=self.base)
        self.assertTrue(is_deny(parsed),
                        "大小寫變異的 fable id 仍應被 gate（is_fable 需 lowercase），stdout=%r" % raw)

    def test_fable_off_reaches_fanout_advisory(self):
        # #20 F6：fable + Workflow + FABLE_WORKFLOW=off + 寬 script → fall-through 到 #19 advisory
        # （釘住 off-path reachability——gate off 時 #19 的 fan-out 建議仍該送達）
        env = dict(self.base, CLAUDE_HOT_LIMIT_FABLE_WORKFLOW="off")
        tp = make_transcript(self.tmp.name, ["claude-fable-5"])
        payload = {"tool_name": "Workflow",
                   "tool_input": {"script": "const r = await parallel(items.map(x=>()=>agent('go')))"},
                   "transcript_path": tp}
        _, parsed, raw = run_hook(payload=payload, env_overrides=env)
        msg = (parsed or {}).get("systemMessage", "") if parsed else ""
        self.assertFalse(is_deny(parsed), "off 模式不該 deny，stdout=%r" % raw)
        self.assertIn("靜態估寬 fan-out", msg,
                      "fable gate off 時應 fall-through 到 #19 fan-out advisory，stdout=%r" % raw)

    def test_fable_pinned_gate_off_still_advises(self):
        # #20 verify DA（最高風險複合 shape）：fable session + FABLE_WORKFLOW=off + 寬 parallel
        # + agent 竟 pin 到 fable 本身 → 若 F4 value-blind 就會整發完全靜默。value-aware 後：
        # pin 到 fable ≠ pin cheap → 不 suppress → #19 advisory 仍送達。
        env = dict(self.base, CLAUDE_HOT_LIMIT_FABLE_WORKFLOW="off")
        tp = make_transcript(self.tmp.name, ["claude-fable-5"])
        payload = {"tool_name": "Workflow",
                   "tool_input": {"script": "const r = await parallel(items.map(x=>()=>agent('go', {model:'claude-fable-5'})))"},
                   "transcript_path": tp}
        _, parsed, raw = run_hook(payload=payload, env_overrides=env)
        msg = (parsed or {}).get("systemMessage", "") if parsed else ""
        self.assertFalse(is_deny(parsed))
        self.assertIn("靜態估寬 fan-out", msg,
                      "fable-pinned + gate off 的寬 fan-out 不該完全靜默（value-aware F4），stdout=%r" % raw)

    # --- #21 F5：Fable session 開 Agent → advisory（不 deny；單一 Agent 非 Workflow-scale fan-out）---

    F5_MARK = "Fable 5 session 開 Agent"

    def _agent_payload(self, agent_model=None):
        tp = make_transcript(self.tmp.name, ["claude-fable-5"])
        ti = {}
        if agent_model is not None:
            ti["model"] = agent_model
        return {"tool_name": "Agent", "tool_input": ti, "transcript_path": tp}

    def test_fable_agent_unpinned_advises(self):
        # F5：fable session + 沒 pin model 的 Agent → subagent 會繼承 fable → advisory（不 deny）
        _, parsed, raw = run_hook(payload=self._agent_payload(), env_overrides=self.base)
        self.assertFalse(is_deny(parsed), "F5 是 advisory 不該 deny（單一 Agent 非 fan-out），stdout=%r" % raw)
        msg = (parsed or {}).get("systemMessage", "") if parsed else ""
        self.assertIn(self.F5_MARK, msg, "fable + unpinned Agent 應提醒 pin subagent model，stdout=%r" % raw)

    def test_fable_agent_pinned_cheap_silent(self):
        # F5：pin 到非-fable model → 不繼承 fable → 靜默
        _, parsed, raw = run_hook(payload=self._agent_payload(agent_model="claude-sonnet-5"), env_overrides=self.base)
        msg = (parsed or {}).get("systemMessage", "") if parsed else ""
        self.assertNotIn(self.F5_MARK, msg, "pin 非-fable model 的 Agent 不繼承 fable → 不提醒，stdout=%r" % raw)

    def test_fable_agent_pinned_fable_advises(self):
        # F5：明確 pin 到 fable → 仍繼承 fable → 提醒
        _, parsed, raw = run_hook(payload=self._agent_payload(agent_model="claude-fable-5"), env_overrides=self.base)
        msg = (parsed or {}).get("systemMessage", "") if parsed else ""
        self.assertIn(self.F5_MARK, msg, "pin 到 fable 的 Agent 仍該提醒，stdout=%r" % raw)

    def test_nonfable_agent_no_f5_advisory(self):
        # F5 反面：非-fable session 開 Agent 不觸發
        tp = make_transcript(self.tmp.name, ["claude-opus-4-8"])
        payload = {"tool_name": "Agent", "tool_input": {}, "transcript_path": tp}
        _, parsed, raw = run_hook(payload=payload, env_overrides=self.base)
        msg = (parsed or {}).get("systemMessage", "") if parsed else ""
        self.assertNotIn(self.F5_MARK, msg, "非-fable session 不該觸發 F5，stdout=%r" % raw)

    def test_fable_agent_nudge_off_suppresses_f5(self):
        # F5：WORKFLOW_NUDGE=0 一併關掉 F5 advisory
        env = dict(self.base, CLAUDE_HOT_LIMIT_WORKFLOW_NUDGE="0")
        _, parsed, raw = run_hook(payload=self._agent_payload(), env_overrides=env)
        msg = (parsed or {}).get("systemMessage", "") if parsed else ""
        self.assertNotIn(self.F5_MARK, msg, "NUDGE=0 應關閉 F5 advisory，stdout=%r" % raw)


class WorkflowFanoutAdvisoryTest(unittest.TestCase):
    """#19 — 依 Workflow fan-out 寬度給 dispatch-model 建議（顯示，不擋）。

    hook 從 tool_input 估 fan-out 寬度：inline `script`（24%）直接 parse；`scriptPath`
    （66%）bounded 讀檔；name/resume（11%）估不到 → 不注入。寬（parallel/pipeline 或
    ≥4 agent()）→ systemMessage 建議 pin sonnet。靜態估（dynamic 估不到），fail-open。
    無 transcript → model unknown → 非 fable → 通過 #18 gate → 到達本段。"""

    WIDE_PARALLEL = "export const meta={};\nconst r = await parallel(items.map(x => () => agent('go')));"
    WIDE_MANY = "agent('a'); agent('b'); agent('c'); agent('d'); agent('e');"
    NARROW = "export const meta={};\nconst x = await agent('single task');"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = {
            "CLAUDE_HOT_LIMIT_DATA": os.path.join(self.tmp.name, "account"),
            "CLAUDE_HOT_LIMIT_MAX": "999",   # 不讓 burst 干擾
            "CLAUDE_HOT_LIMIT_MIN_GAP": "0",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _wf(self, script=None, script_path=None, tool="Workflow"):
        ti = {}
        if script is not None:
            ti["script"] = script
        if script_path is not None:
            ti["scriptPath"] = script_path
        return {"tool_name": tool, "tool_input": ti}  # 無 transcript_path → model unknown

    def _msg(self, parsed):
        return (parsed or {}).get("systemMessage", "") if parsed else ""

    def test_wide_inline_parallel_advises_sonnet(self):
        _, parsed, raw = run_hook(payload=self._wf(script=self.WIDE_PARALLEL), env_overrides=self.base)
        self.assertFalse(is_deny(parsed), "純顯示、不該 deny，stdout=%r" % raw)
        self.assertIn("sonnet", self._msg(parsed), "寬 fan-out 應建議 pin sonnet，stdout=%r" % raw)

    def test_wide_inline_many_agents_advises_sonnet(self):
        _, parsed, raw = run_hook(payload=self._wf(script=self.WIDE_MANY), env_overrides=self.base)
        self.assertIn("sonnet", self._msg(parsed), "≥4 agent() 應算寬並建議 sonnet，stdout=%r" % raw)

    def test_narrow_inline_no_advisory(self):
        _, parsed, raw = run_hook(payload=self._wf(script=self.NARROW), env_overrides=self.base)
        self.assertNotIn("sonnet", self._msg(parsed), "窄 fan-out 不該建議切 model，stdout=%r" % raw)

    def test_wide_scriptpath_read_from_file(self):
        sp = os.path.join(self.tmp.name, "wf.js")
        with open(sp, "w") as f:
            f.write(self.WIDE_PARALLEL)
        _, parsed, raw = run_hook(payload=self._wf(script_path=sp), env_overrides=self.base)
        self.assertIn("sonnet", self._msg(parsed), "scriptPath 也應讀檔估寬度並建議，stdout=%r" % raw)

    def test_narrow_scriptpath_no_advisory(self):
        sp = os.path.join(self.tmp.name, "narrow.js")
        with open(sp, "w") as f:
            f.write(self.NARROW)
        _, parsed, raw = run_hook(payload=self._wf(script_path=sp), env_overrides=self.base)
        self.assertNotIn("sonnet", self._msg(parsed), "窄 scriptPath 不該建議，stdout=%r" % raw)

    def test_name_only_fail_open_no_advisory(self):
        # 無 script / scriptPath（name / resume）→ 估不到 → 不注入（fail-open）
        _, parsed, raw = run_hook(payload=self._wf(), env_overrides=self.base)
        self.assertNotIn("sonnet", self._msg(parsed), "估不到寬度時應 fail-open 不注入，stdout=%r" % raw)

    def test_missing_scriptpath_file_fail_open(self):
        _, parsed, raw = run_hook(payload=self._wf(script_path="/nonexistent/wf.js"), env_overrides=self.base)
        self.assertFalse(is_deny(parsed))
        self.assertNotIn("sonnet", self._msg(parsed), "scriptPath 讀不到應 fail-open，stdout=%r" % raw)

    def test_nudge_disabled_by_env_suppresses_advisory(self):
        env = dict(self.base, CLAUDE_HOT_LIMIT_WORKFLOW_NUDGE="0")
        _, parsed, raw = run_hook(payload=self._wf(script=self.WIDE_PARALLEL), env_overrides=env)
        self.assertNotIn("sonnet", self._msg(parsed), "NUDGE=0 應關閉寬度建議，stdout=%r" % raw)

    def test_agent_tool_not_advised(self):
        _, parsed, raw = run_hook(payload=self._wf(script=self.WIDE_PARALLEL, tool="Agent"), env_overrides=self.base)
        self.assertNotIn("sonnet", self._msg(parsed), "只對 Workflow 建議，Agent 不觸發，stdout=%r" % raw)

    # --- verify-driven follow-up fixes（6-AI ensemble 抓到；#19 F1/F2/F3）---

    # F1 caveat 的辨識標記（wide advisory 不含此字串，只有 narrow-but-uncertain 才印）
    CAVEAT_MARK = "估看似窄"

    # F2 — 註解裡的 agent( 不該被數成呼叫
    COMMENT_ONLY_AGENTS = (
        "// example: agent('a'); agent('b'); agent('c'); agent('d');\n"
        "export const meta={};\nconst x = await agent('one real task');"
    )
    # F2 — 字串字面裡的 agent( 不該被數成呼叫
    STRING_LITERAL_AGENTS = (
        "const doc = 'call agent() then agent() then agent() then agent()';\n"
        "const x = await agent('one real task');"
    )
    # F1 — 慣用 dynamic fan-out：Promise.all + .map + 單一 literal agent(（靜態只數到 1）
    DYNAMIC_LOOP = (
        "export const meta={};\n"
        "const rs = await Promise.all(tasks.map(t => agent('go ' + t)));"
    )

    def test_comment_agents_not_counted(self):
        # F2：4 個 agent( 在註解裡 + 1 個真呼叫 → 剝除後只剩 1（窄）→ 不該誤報寬
        _, parsed, raw = run_hook(payload=self._wf(script=self.COMMENT_ONLY_AGENTS), env_overrides=self.base)
        self.assertFalse(is_deny(parsed))
        self.assertNotIn("sonnet", self._msg(parsed), "註解裡的 agent( 不該被數成寬 fan-out，stdout=%r" % raw)

    def test_string_literal_agents_not_counted(self):
        # F2：字串字面裡的 agent( 同樣不該被數
        _, parsed, raw = run_hook(payload=self._wf(script=self.STRING_LITERAL_AGENTS), env_overrides=self.base)
        self.assertNotIn("sonnet", self._msg(parsed), "字串裡的 agent( 不該被數成寬 fan-out，stdout=%r" % raw)

    def test_dynamic_loop_flagged_uncertain(self):
        # F1：Promise.all/map 動態 fan-out 靜態估看似窄 → 應印「可能更寬」的 caveat（silence 是假安心）
        _, parsed, raw = run_hook(payload=self._wf(script=self.DYNAMIC_LOOP), env_overrides=self.base)
        self.assertFalse(is_deny(parsed))
        self.assertIn(self.CAVEAT_MARK, self._msg(parsed),
                      "dynamic loop fan-out 不該完全靜默，應標示靜態估看不到，stdout=%r" % raw)

    def test_plain_narrow_stays_silent(self):
        # F1 反面：真的窄（無 dynamic 跡象、無截斷）→ 完全不印 caveat（避免 advisory fatigue）
        _, parsed, raw = run_hook(payload=self._wf(script=self.NARROW), env_overrides=self.base)
        self.assertNotIn(self.CAVEAT_MARK, self._msg(parsed),
                         "真的窄的 script 不該印 dynamic caveat，stdout=%r" % raw)

    def test_truncated_scriptpath_flagged_uncertain(self):
        # F3：scriptPath 檔 > 200KB（head-read 截斷）→ 就算 head 看似窄也標示不確定
        sp = os.path.join(self.tmp.name, "big.js")
        with open(sp, "w") as f:
            f.write("x" * 201000)   # 超過 _WORKFLOW_SCRIPT_MAX_BYTES → 讀取截斷
        _, parsed, raw = run_hook(payload=self._wf(script_path=sp), env_overrides=self.base)
        self.assertFalse(is_deny(parsed))
        self.assertIn(self.CAVEAT_MARK, self._msg(parsed),
                      "過大 scriptPath 被截斷時應標示估算不完整，stdout=%r" % raw)

    # --- #20 (F4/F5/F8/F9): 精度 + 可調門檻 ---

    def _agents(self, n, model=None):
        """產生 n 個 agent() 呼叫的 script。model 給定 → 每個帶 {model:'<model>'}。"""
        call = ("agent('t%%d', {model:'%s'})" % model) if model else "agent('t%d')"
        return "; ".join(call % i for i in range(n)) + ";"

    def test_threshold_env_raises_wide_bar(self):
        # F5：門檻可用 env 調高——min=6 時 4 個 agent 不再算寬
        env = dict(self.base, CLAUDE_HOT_LIMIT_FANOUT_WIDE_MIN="6")
        _, parsed, raw = run_hook(payload=self._wf(script=self._agents(4)), env_overrides=env)
        self.assertNotIn("sonnet", self._msg(parsed), "門檻=6 時 4 個 agent 不算寬，stdout=%r" % raw)

    def test_threshold_env_boundary(self):
        # F5：門檻=6 的邊界 — 5 narrow / 6 wide
        env = dict(self.base, CLAUDE_HOT_LIMIT_FANOUT_WIDE_MIN="6")
        _, p5, r5 = run_hook(payload=self._wf(script=self._agents(5)), env_overrides=env)
        self.assertNotIn("sonnet", self._msg(p5), "門檻=6：5 個 agent narrow，stdout=%r" % r5)
        _, p6, r6 = run_hook(payload=self._wf(script=self._agents(6)), env_overrides=env)
        self.assertIn("sonnet", self._msg(p6), "門檻=6：6 個 agent wide，stdout=%r" % r6)

    def test_threshold_file_override(self):
        # F5：<data_dir>/fanout-wide-min 檔案 override（mid-session 生效）
        os.makedirs(self.base["CLAUDE_HOT_LIMIT_DATA"], exist_ok=True)
        with open(os.path.join(self.base["CLAUDE_HOT_LIMIT_DATA"], "fanout-wide-min"), "w") as f:
            f.write("6")
        _, parsed, raw = run_hook(payload=self._wf(script=self._agents(4)), env_overrides=self.base)
        self.assertNotIn("sonnet", self._msg(parsed), "檔案門檻=6 時 4 個 agent narrow，stdout=%r" % raw)

    def test_threshold_file_beats_env(self):
        # F5：檔案 override 優先於 env（比照 file_override_int 契約）
        os.makedirs(self.base["CLAUDE_HOT_LIMIT_DATA"], exist_ok=True)
        with open(os.path.join(self.base["CLAUDE_HOT_LIMIT_DATA"], "fanout-wide-min"), "w") as f:
            f.write("6")
        env = dict(self.base, CLAUDE_HOT_LIMIT_FANOUT_WIDE_MIN="2")  # env 說 2（4 會 wide），檔案說 6（narrow）
        _, parsed, raw = run_hook(payload=self._wf(script=self._agents(4)), env_overrides=env)
        self.assertNotIn("sonnet", self._msg(parsed), "檔案門檻應優先於 env，stdout=%r" % raw)

    def test_already_pinned_wide_suppresses_advisory(self):
        # F4：寬 script 但每個 agent() 都已 pin 便宜 model（sonnet）→ 不再嘮叨 pin
        _, parsed, raw = run_hook(payload=self._wf(script=self._agents(4, model='sonnet')), env_overrides=self.base)
        self.assertNotIn("sonnet", self._msg(parsed),
                         "已 pin cheap model 的寬 script 不該再建議 pin（advisory fatigue），stdout=%r" % raw)

    def test_unpinned_wide_still_advises(self):
        # F4 反面：沒 pin 的寬 script 照樣提醒（別把 F4 做過頭）
        _, parsed, raw = run_hook(payload=self._wf(script=self._agents(4)), env_overrides=self.base)
        self.assertIn("sonnet", self._msg(parsed), "沒 pin 的寬 script 仍應建議 pin，stdout=%r" % raw)

    # --- #20 verify-hardening（F4 value-aware + call-site-bound；F5 clamp；Codex uncertain）---

    def test_pinned_to_expensive_still_advises(self):
        # F4 value-aware：pin 到貴 model（fable/opus）不算「已 pin cheap」→ 仍該提醒 pin sonnet
        _, parsed, raw = run_hook(payload=self._wf(script=self._agents(4, model='claude-fable-5')), env_overrides=self.base)
        self.assertIn("sonnet", self._msg(parsed),
                      "pin 到貴 model 的寬 script 仍應建議 pin cheap，stdout=%r" % raw)

    def test_comment_model_key_no_spoof(self):
        # F4 call-site-bound：註解裡的 model: 不該假 suppress 一個真的 unpinned 寬 fan-out
        script = self._agents(4) + "\n// model: model: model: model:"
        _, parsed, raw = run_hook(payload=self._wf(script=script), env_overrides=self.base)
        self.assertIn("sonnet", self._msg(parsed),
                      "註解裡的 model: 不該假 suppress advisory（call-site-bound），stdout=%r" % raw)

    def test_threshold_zero_clamped(self):
        # F5 guard：FANOUT_WIDE_MIN=0 不該讓 0-agent script 觸發「0 個 agent」advisory
        env = dict(self.base, CLAUDE_HOT_LIMIT_FANOUT_WIDE_MIN="0")
        _, parsed, raw = run_hook(payload=self._wf(script="const x = 1;"), env_overrides=env)
        self.assertNotIn("sonnet", self._msg(parsed),
                         "門檻=0 應被 clamp 到 ≥1，0 個 agent 不該觸發，stdout=%r" % raw)

    def test_wide_uncertain_pinned_still_warns(self):
        # Codex LOW：wide + uncertain(truncated) + head 看似 all-pinned → 不該 suppress（看不全）
        sp = os.path.join(self.tmp.name, "big_pinned.js")
        with open(sp, "w") as f:
            f.write("await parallel(x);\n" + self._agents(3, model='sonnet') + "\n" + "x" * 201000)
        _, parsed, raw = run_hook(payload=self._wf(script_path=sp), env_overrides=self.base)
        msg = self._msg(parsed)
        self.assertTrue("sonnet" in msg or self.CAVEAT_MARK in msg,
                        "wide+uncertain 時不該因 head 看似 pinned 就靜默，stdout=%r" % raw)

    def test_sub_agent_counted_as_fanout(self):
        # F8：sub_agent( 也算 fan-out（原 \bagent 因 _ 無邊界而漏掉）
        script = "; ".join("sub_agent('t%d')" % i for i in range(4)) + ";"
        _, parsed, raw = run_hook(payload=self._wf(script=script), env_overrides=self.base)
        self.assertIn("sonnet", self._msg(parsed), "sub_agent( 應被算進 fan-out（F8），stdout=%r" % raw)

    def test_advisory_shows_agent_count(self):
        # F9：advisory 顯示確切 agent 數字（原測試只斷言 'sonnet' 子字串）
        _, parsed, raw = run_hook(payload=self._wf(script=self._agents(5)), env_overrides=self.base)
        msg = self._msg(parsed)
        self.assertIn("sonnet", msg)
        self.assertIn("5 個 agent", msg, "advisory 應顯示確切 agent 數（F9），stdout=%r" % raw)


class SessionFableNudgeTest(unittest.TestCase):
    """#24 (b) — SessionStart best-effort advisory：fable session → coordinator-burn nudge。

    誠實邊界：resume/compact（transcript 有 fable turns）抓得到；fresh startup（空 transcript）
    測不到 → 靜默。fail-open + 同受 _WORKFLOW_NUDGE / _OFF 開關。"""

    HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "hooks", "session-fable-nudge.py")
    MARK = "這是 Fable 5 session"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, models, env_overrides=None, transcript_path=None):
        tp = transcript_path if transcript_path is not None else make_transcript(self.tmp.name, models)
        payload = {"hook_event_name": "SessionStart", "source": "compact", "transcript_path": tp}
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_HOT_LIMIT_")}
        if env_overrides:
            env.update(env_overrides)
        p = subprocess.run([sys.executable, self.HOOK], input=json.dumps(payload),
                           capture_output=True, text=True, env=env)
        return p.stdout

    def test_fable_session_nudges(self):
        self.assertIn(self.MARK, self._run(["claude-fable-5"]))

    def test_uppercase_fable_nudges(self):
        # is_fable lower-normalize（同 pacing-guard）
        self.assertIn(self.MARK, self._run(["Claude-Fable-5"]))

    def test_nonfable_session_silent(self):
        self.assertNotIn(self.MARK, self._run(["claude-opus-4-8"]))

    def test_no_model_silent(self):
        # 只有 <synthetic> turn（≈ fresh startup）→ 測不到 model → 靜默 no-op
        self.assertNotIn(self.MARK, self._run([None]))

    def test_nudge_off_suppresses(self):
        self.assertNotIn(self.MARK, self._run(["claude-fable-5"], {"CLAUDE_HOT_LIMIT_WORKFLOW_NUDGE": "0"}))

    def test_global_off_suppresses(self):
        self.assertNotIn(self.MARK, self._run(["claude-fable-5"], {"CLAUDE_HOT_LIMIT_OFF": "1"}))

    def test_missing_transcript_silent(self):
        self.assertNotIn(self.MARK, self._run(None, transcript_path="/nonexistent/t.jsonl"))

    def test_empty_stdin_fail_open(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_HOT_LIMIT_")}
        p = subprocess.run([sys.executable, self.HOOK], input="", capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, "空 stdin 應 fail-open exit 0")
        self.assertNotIn(self.MARK, p.stdout)


class UnifiedUtilizationHeatTest(unittest.TestCase):
    """#25 — 官方 utilization leading indicator + null-blindness 修正。

    #12（v1.15.0）後每筆 Max/OAuth record 帶 rl_unified_5h_utilization（帳號級 0-1）
    + status + reset epoch。rate_state_heat 應優先消費官方水位；且全 null record
    （Max 的 API-platform 六欄恆 null 的 pre-1.15 形狀）不得誤判「確認冷」壓制
    429 fallback（既有 null-blindness bug）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = os.path.join(self.tmp.name, "ledger")
        os.makedirs(self.data, exist_ok=True)
        self.env = {
            "CLAUDE_HOT_LIMIT_DATA": self.data,
            "CLAUDE_HOT_LIMIT_MAX": "999",
            "CLAUDE_HOT_LIMIT_MIN_GAP": "0",
            "CLAUDE_HOT_LIMIT_WINDOW": "600",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def fire(self, tool="Workflow", extra=None):
        return run_hook(tool=tool, env_overrides=dict(self.env, **(extra or {})))

    def seed_rate_state(self, age=5, **fields):
        row = {
            "ts": time.time() - age,
            "rl_requests_remaining": None, "rl_requests_reset": None,
            "rl_input_tokens_remaining": None, "rl_input_tokens_reset": None,
            "rl_output_tokens_remaining": None, "rl_output_tokens_reset": None,
            "usage": None,
        }
        row.update(fields)
        with open(os.path.join(self.data, "rate-state.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")

    def seed_hot_trip(self, age=5):
        with open(os.path.join(self.data, "trips-raw.jsonl"), "a") as f:
            f.write(json.dumps({"recorded_at": time.time() - age,
                                "payload": {"error": "rate_limit"}}) + "\n")

    def test_all_null_record_falls_back_to_trips_raw(self):
        # null-blindness 修正鑑別：全 null record（pre-1.15 Max 形狀）+ 近期真撞牆
        # → 必須出聲（現行 bug：全 null 誤回「確認冷」→ 429 fallback 被壓制 → 靜默）。
        self.seed_rate_state(age=5)  # 全預設 null
        self.seed_hot_trip(age=5)
        _, parsed, raw = self.fire()
        self.assertIsNotNone(parsed,
                             "全 null 快照不是「確認冷」——應 fallback 到 trips-raw 出聲，stdout=%r" % raw)
        self.assertIn("撞牆", parsed.get("systemMessage", ""),
                      "fallback 訊息應是 429 啟發式那條，msg=%r" % parsed.get("systemMessage"))

    def test_nudges_on_official_utilization_above_threshold(self):
        self.seed_rate_state(age=5, rl_unified_5h_utilization=0.85,
                             rl_unified_5h_status="allowed")
        _, parsed, raw = self.fire()
        self.assertIsNotNone(parsed, "水位 0.85 ≥ 預設門檻 0.80 應出聲，stdout=%r" % raw)
        msg = parsed.get("systemMessage", "")
        self.assertIn("5h 配額水位", msg, "應點名官方 5h 水位，msg=%r" % msg)
        self.assertIn("85", msg, "應帶實際水位數字，msg=%r" % msg)

    def test_allowed_warning_status_triggers_directly(self):
        # server 直判優先於本地門檻：水位 0.5（< 0.8）但 status=allowed_warning → 熱
        self.seed_rate_state(age=5, rl_unified_5h_utilization=0.5,
                             rl_unified_5h_status="allowed_warning")
        _, parsed, raw = self.fire()
        self.assertIsNotNone(parsed, "allowed_warning 應直判熱（官方已標警告），stdout=%r" % raw)
        self.assertIn("allowed_warning", parsed.get("systemMessage", ""),
                      "應點名官方 warning 狀態，msg=%r" % parsed.get("systemMessage"))

    def test_rejected_status_triggers_directly(self):
        # 5h_status=rejected（撞牆中）——任何非 allowed 狀態都直判熱，不限 allowed_warning
        self.seed_rate_state(age=5, rl_unified_5h_utilization=1.0,
                             rl_unified_5h_status="rejected")
        _, parsed, raw = self.fire()
        self.assertIsNotNone(parsed, "rejected 應直判熱，stdout=%r" % raw)
        self.assertIn("rejected", parsed.get("systemMessage", ""))

    def test_utilization_below_threshold_confirms_cold(self):
        # 官方水位低 + status allowed → 真冷，取代 429 啟發式（沿既有「取代不疊加」語意）
        self.seed_rate_state(age=5, rl_unified_5h_utilization=0.25,
                             rl_unified_5h_status="allowed")
        self.seed_hot_trip(age=5)
        _, parsed, raw = self.fire()
        self.assertEqual(raw, "", "官方水位 0.25 確認冷應安靜（即使 trips 有舊撞牆），stdout=%r" % raw)

    def test_util_warn_file_flag_beats_env(self):
        self.seed_rate_state(age=5, rl_unified_5h_utilization=0.5,
                             rl_unified_5h_status="allowed")
        # env 說 0.3（會觸發）、檔案說 0.9（不觸發）→ 檔案優先 → 靜默
        with open(os.path.join(self.data, "util-warn"), "w") as f:
            f.write("0.9")
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_UTIL_WARN": "0.3"})
        self.assertEqual(raw, "", "util-warn 檔案旗標應優先於 env，stdout=%r" % raw)
        os.remove(os.path.join(self.data, "util-warn"))
        _, parsed, raw = self.fire(extra={"CLAUDE_HOT_LIMIT_UTIL_WARN": "0.3"})
        self.assertIsNotNone(parsed, "移除檔案後 env 0.3 生效（0.5 ≥ 0.3）應出聲，stdout=%r" % raw)

    def test_replay_production_high_watermark_record(self):
        # 2026-07-11 13:57 真實 production record 形狀原樣 replay（0.99 + allowed_warning）
        self.seed_rate_state(
            age=5, model="claude-opus-4-8", status=200,
            rl_unified_5h_utilization=0.99, rl_unified_5h_status="allowed_warning",
            rl_unified_5h_reset=int(time.time()) + 7980,
            rl_unified_7d_utilization=0.19, rl_unified_7d_status="allowed",
            rl_unified_7d_reset=int(time.time()) + 500000,
            rl_unified_representative_claim="five_hour",
            rl_unified_status="allowed_warning", rl_unified_reset=int(time.time()) + 7980,
            rl_unified_overage_status="rejected",
            rl_unified_overage_disabled_reason="org_level_disabled",
            rl_unified_overage_fallback_percentage=0.5,
            usage={"input_tokens": 4, "output_tokens": 128})
        _, parsed, raw = self.fire()
        self.assertIsNotNone(parsed, "production 0.99 record 應出聲，stdout=%r" % raw)
        self.assertIn("99", parsed.get("systemMessage", ""),
                      "應帶 99%% 水位，msg=%r" % parsed.get("systemMessage"))

    def test_tail_read_reaches_last_record_in_large_file(self):
        # 檔案 > tail 上限（256KB）：新讀取只掃檔尾——最後一筆熱 record 必須被讀到。
        filler = {"ts": time.time() - 30, "model": "claude-opus-4-8",
                  "rl_unified_5h_utilization": 0.1, "rl_unified_5h_status": "allowed"}
        line = json.dumps(filler) + "\n"
        n = (300 * 1024 // len(line)) + 1  # ~300KB
        with open(os.path.join(self.data, "rate-state.jsonl"), "w") as f:
            for _ in range(n):
                f.write(line)
        self.seed_rate_state(age=2, rl_unified_5h_utilization=0.95,
                             rl_unified_5h_status="allowed")
        _, parsed, raw = self.fire()
        self.assertIsNotNone(parsed, "大檔下最後一筆 0.95 應被 tail-read 讀到並出聲，stdout=%r" % raw)
        self.assertIn("95", parsed.get("systemMessage", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
