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


if __name__ == "__main__":
    unittest.main(verbosity=2)
