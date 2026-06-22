#!/usr/bin/env python3
"""
claude-hot-limit · trip-recorder 黑箱測試（StopFailure hook）

撞到 429/529 時 Claude Code 會 fire StopFailure（error_type=rate_limit/overloaded）。
這支 hook 在那當下讀帳本、把 trip 自動記進 calibration-log.md。黑箱測：餵 StopFailure
payload + 已 seed 的帳本，檢查 log 多了一列、且 fail-open。

跑法: python3 tests/test_trip_recorder.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "trip-recorder.py")


def run_hook(payload, env_overrides):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_HOT_LIMIT_") and k != "CLAUDE_PLUGIN_DATA"}
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, HOOK],
        input=json.dumps(payload) if isinstance(payload, (dict, list)) else payload,
        capture_output=True, text=True, env=env, timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


class TripRecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        # seed 帳本：近期 5 發 launch
        now = time.time()
        with open(os.path.join(self.data, "launches.jsonl"), "w") as f:
            for dt in (2, 8, 30, 120, 400):
                f.write(json.dumps({"ts": now - dt, "tool": "Agent"}) + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def log_text(self):
        p = os.path.join(self.data, "calibration-log.md")
        return open(p).read() if os.path.exists(p) else ""

    def test_records_overloaded_trip_with_counts(self):
        code, _, _ = run_hook(
            {"hook_event_name": "StopFailure", "error_type": "overloaded"},
            {"CLAUDE_HOT_LIMIT_DATA": self.data},
        )
        self.assertEqual(code, 0, "hook 應 exit 0（StopFailure 輸出被忽略，但別 crash）")
        txt = self.log_text()
        self.assertIn("overloaded", txt, "log 應記下 error_type")
        self.assertIn("[auto]", txt, "auto 記錄應標記 [auto] 與手動區隔")
        # 視窗計數：近60s=3(2,8,30)、近600s=5。row 應含這兩個數
        last = [l for l in txt.splitlines() if "[auto] overloaded" in l][-1]
        cells = [c.strip() for c in last.strip("|").split("|")]
        self.assertEqual(cells[2], "3", "近60s 應為 3，row=%r" % last)
        self.assertEqual(cells[-1], "5", "近600s 應為 5，row=%r" % last)

    def test_fail_open_on_bad_stdin(self):
        code, out, _ = run_hook("not json", {"CLAUDE_HOT_LIMIT_DATA": self.data})
        self.assertEqual(code, 0, "壞 stdin 要 fail-open（exit 0）")

    def test_creates_log_header_when_missing(self):
        # log 不存在時要自建表頭
        self.assertNotIn("觀測紀錄", self.log_text())
        run_hook({"hook_event_name": "StopFailure", "error_type": "rate_limit"},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        self.assertIn("|", self.log_text(), "應建出表格")


if __name__ == "__main__":
    unittest.main(verbosity=2)
