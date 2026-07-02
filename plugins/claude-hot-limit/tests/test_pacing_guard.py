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


if __name__ == "__main__":
    unittest.main(verbosity=2)
