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
        # 視窗計數：近60s=3(2,8,30)、近600s=5。row 應含這兩個數。
        # #5 後最後一欄是 model → 近600s 移到 cells[-2]，model（無 transcript → unknown）在 cells[-1]。
        last = [l for l in txt.splitlines() if "[auto] overloaded" in l][-1]
        cells = [c.strip() for c in last.strip("|").split("|")]
        self.assertEqual(cells[2], "3", "近60s 應為 3，row=%r" % last)
        self.assertEqual(cells[-2], "5", "近600s 應為 5（model 欄後移一格），row=%r" % last)
        self.assertEqual(cells[-1], "unknown", "無 transcript → model 欄記 unknown，row=%r" % last)

    def test_fail_open_on_bad_stdin(self):
        code, out, _ = run_hook("not json", {"CLAUDE_HOT_LIMIT_DATA": self.data})
        self.assertEqual(code, 0, "壞 stdin 要 fail-open（exit 0）")

    def test_creates_log_header_when_missing(self):
        # log 不存在時要自建表頭
        self.assertNotIn("觀測紀錄", self.log_text())
        run_hook({"hook_event_name": "StopFailure", "error_type": "rate_limit"},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        self.assertIn("|", self.log_text(), "應建出表格")

    def test_null_error_type_recorded_as_unknown(self):
        # 實測：StopFailure 的 error_type 可能是 null/None（退化路徑）。
        # matcher 已放寬為 .*，腳本必須把 None 正規化成 unknown 並仍記下（ambiguous → 寧記勿漏）
        code, _, _ = run_hook(
            {"hook_event_name": "StopFailure", "error_type": None},
            {"CLAUDE_HOT_LIMIT_DATA": self.data},
        )
        self.assertEqual(code, 0)
        txt = self.log_text()
        self.assertIn("[auto] unknown", txt, "None error_type 應記成 unknown，txt=%r" % txt)
        self.assertNotIn("[auto] None", txt, "不該出現字面 None")

    def test_skips_clearly_non_ratelimit_type(self):
        # 明確非 rate-limit 的 API error（auth/billing/model…）不該污染校準 log
        code, _, _ = run_hook(
            {"hook_event_name": "StopFailure", "error_type": "billing_error"},
            {"CLAUDE_HOT_LIMIT_DATA": self.data},
        )
        self.assertEqual(code, 0)
        self.assertNotIn("[auto]", self.log_text(), "billing_error 不應記成 trip")

    def test_records_real_error_field(self):
        # 實測 131 筆 StopFailure payload：欄位叫 `error`（不是 `error_type`，後者根本不存在）。
        # 真值在 error：rate_limit / server_error / invalid_request。hook 必須讀得到。
        code, _, _ = run_hook(
            {"hook_event_name": "StopFailure", "error": "rate_limit"},
            {"CLAUDE_HOT_LIMIT_DATA": self.data},
        )
        self.assertEqual(code, 0)
        txt = self.log_text()
        self.assertIn("[auto] rate_limit", txt,
                      "真實 error 欄位應被記成 rate_limit（不是 unknown），txt=%r" % txt)
        self.assertNotIn("[auto] unknown", txt, "有明確 error 時不該退化成 unknown")

    def test_skips_via_real_error_field(self):
        # SKIP 過濾也必須吃真實 error 欄位：invalid_request 不是撞牆，不該污染校準 log
        run_hook({"hook_event_name": "StopFailure", "error": "invalid_request"},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        self.assertNotIn("[auto]", self.log_text(),
                         "invalid_request（經真實 error 欄位）應被 skip")

    def raw_rows(self):
        p = os.path.join(self.data, "trips-raw.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def test_dumps_full_raw_payload(self):
        # 不信任 error_type / UI 訊息 → 把整包 StopFailure payload 原封不動落地，事後看真實欄位
        payload = {
            "hook_event_name": "StopFailure", "error_type": "overloaded",
            "retry_after": 42, "request_id": "req_abc",
            "message": "Server is temporarily limiting requests",
        }
        run_hook(payload, {"CLAUDE_HOT_LIMIT_DATA": self.data})
        rows = self.raw_rows()
        self.assertEqual(len(rows), 1, "應產生一筆 trips-raw.jsonl")
        self.assertIn("recorded_at", rows[-1])
        pl = rows[-1]["payload"]
        self.assertEqual(pl.get("retry_after"), 42, "完整欄位都要在，pl=%r" % pl)
        self.assertEqual(pl.get("request_id"), "req_abc")
        self.assertIn("message", pl)

    def test_raw_dump_captures_even_skipped_types(self):
        # 即使會被 calibration log skip 的型別，原始 payload 仍要落地（診斷不過濾）
        run_hook({"hook_event_name": "StopFailure", "error_type": "billing_error"},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        rows = self.raw_rows()
        self.assertEqual(rows[-1]["payload"].get("error_type"), "billing_error",
                         "skip 型別的原始 payload 仍要抓到")
        self.assertNotIn("[auto]", self.log_text(), "但 calibration log 仍 skip")


class PerModelTripRecordTest(unittest.TestCase):
    """#2 — trips-raw.jsonl 每筆 trip 記錄應標註是哪個 model 撞的牆。

    StopFailure payload 帶 transcript_path（131 筆真實 payload 已驗證），用與 pacing-guard
    v1.4.0 同一套 transcript-tail 手法偵測；偵測失敗 → "unknown"（fail-open）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def raw_rows(self):
        p = os.path.join(self.data, "trips-raw.jsonl")
        return [json.loads(l) for l in open(p)] if os.path.exists(p) else []

    def make_transcript(self, models):
        """假 transcript JSONL；None → <synthetic>（應被跳過）。回傳路徑。"""
        path = os.path.join(self.data, "transcript.jsonl")
        with open(path, "w") as f:
            for m in models:
                value = m if m is not None else "<synthetic>"
                f.write(json.dumps({"type": "assistant", "message": {"model": value}}) + "\n")
        return path

    def test_records_model_from_transcript(self):
        tp = self.make_transcript(["claude-sonnet-5"])
        run_hook({"hook_event_name": "StopFailure", "error": "rate_limit",
                  "transcript_path": tp},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        rows = self.raw_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[-1].get("model"), "claude-sonnet-5",
                          "trip 記錄應標註撞牆的 model，row=%r" % rows[-1])

    def test_synthetic_skipped_finds_real_model(self):
        tp = self.make_transcript(["claude-opus-4-8", None])
        run_hook({"hook_event_name": "StopFailure", "error": "rate_limit",
                  "transcript_path": tp},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        rows = self.raw_rows()
        self.assertEqual(rows[-1].get("model"), "claude-opus-4-8",
                          "<synthetic> 佔位應被跳過、往前找真實 model，row=%r" % rows[-1])

    def test_missing_transcript_records_unknown(self):
        run_hook({"hook_event_name": "StopFailure", "error": "rate_limit",
                  "transcript_path": os.path.join(self.data, "does-not-exist.jsonl")},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        rows = self.raw_rows()
        self.assertEqual(rows[-1].get("model"), "unknown",
                          "transcript 讀不到應 fail-open 記 unknown，row=%r" % rows[-1])

    def test_no_transcript_path_records_unknown(self):
        run_hook({"hook_event_name": "StopFailure", "error": "rate_limit"},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        rows = self.raw_rows()
        self.assertEqual(rows[-1].get("model"), "unknown",
                          "payload 缺 transcript_path 應記 unknown，row=%r" % rows[-1])

    def test_non_dict_json_line_in_transcript_does_not_crash(self):
        # Verify 叢集 B（DA repro 1）：transcript 內含合法但非 dict 的 JSON 行（bare 12345），
        # detect_model 不可 crash——必須跳過壞行、往前找到真實 model，trip 照常記錄。
        path = os.path.join(self.data, "transcript.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5"}}) + "\n")
            f.write("12345\n")  # 合法 JSON、非 dict → 舊版在 o.get() 拋 AttributeError
        code, _, _ = run_hook({"hook_event_name": "StopFailure", "error": "rate_limit",
                               "transcript_path": path},
                              {"CLAUDE_HOT_LIMIT_DATA": self.data})
        self.assertEqual(code, 0, "非 dict JSON 行不該讓 recorder crash")
        rows = self.raw_rows()
        self.assertEqual(len(rows), 1, "trip 必須照常記錄（dump 先於一切）")
        self.assertEqual(rows[-1].get("model"), "claude-sonnet-5",
                          "應跳過壞行、往前找到真實 model，row=%r" % rows[-1])

    def test_non_dict_message_in_transcript_does_not_crash(self):
        # message 欄位是字串而非 dict → 舊版 (o.get("message") or {}).get 拋 AttributeError
        path = os.path.join(self.data, "transcript.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"model": "claude-opus-4-8"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": "plain string"}) + "\n")
        code, _, _ = run_hook({"hook_event_name": "StopFailure", "error": "rate_limit",
                               "transcript_path": path},
                              {"CLAUDE_HOT_LIMIT_DATA": self.data})
        self.assertEqual(code, 0)
        rows = self.raw_rows()
        self.assertEqual(rows[-1].get("model"), "claude-opus-4-8")

    def test_fifo_transcript_does_not_block_dump(self):
        # Re-verify finding 3：transcript_path 指向 FIFO（無 writer 的 open 永久 block）時，
        # model 偵測不可卡住 raw dump——「dump 先於一切」也要防 blocking I/O，不只防 exception。
        fifo = os.path.join(self.data, "transcript.fifo")
        os.mkfifo(fifo)
        code, _, _ = run_hook({"hook_event_name": "StopFailure", "error": "rate_limit",
                               "transcript_path": fifo},
                              {"CLAUDE_HOT_LIMIT_DATA": self.data})
        self.assertEqual(code, 0, "FIFO transcript 不該 block（run_hook timeout=30 會抓到）")
        rows = self.raw_rows()
        self.assertEqual(len(rows), 1, "trip 必須照常記錄")
        self.assertEqual(rows[-1].get("model"), "unknown", "非一般檔案 → fail-open 記 unknown")

    def test_non_dict_stdin_payload_still_dumped(self):
        # Verify 叢集 B（DA repro 2）：stdin 是合法 JSON 但非 dict（[1,2,3]）——
        # 改動前能正常 dump（json.dumps 接受任何型別、payload.get 在 dump 之後），
        # 必須恢復這個行為：dump 先於一切，不可在 dump 前 crash。
        code, _, _ = run_hook([1, 2, 3], {"CLAUDE_HOT_LIMIT_DATA": self.data})
        self.assertEqual(code, 0, "非 dict payload 不該 crash")
        rows = self.raw_rows()
        self.assertEqual(len(rows), 1, "原始 payload 仍要 dump（審計軌跡不可丟）")
        self.assertEqual(rows[-1]["payload"], [1, 2, 3])


class CalibrationLogModelColumnTest(unittest.TestCase):
    """#5 — calibration-log.md 校準表加 model 欄（最後一欄）。新檔表頭含該欄；既有舊表頭
    檔一次性遷移表頭+分隔線、歷史資料列原封不動。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.log = os.path.join(self.data, "calibration-log.md")

    def tearDown(self):
        self.tmp.cleanup()

    def log_text(self):
        return open(self.log).read() if os.path.exists(self.log) else ""

    def make_transcript(self, model):
        path = os.path.join(self.data, "transcript.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"model": model}}) + "\n")
        return path

    def header_line(self, txt):
        for l in txt.splitlines():
            if l.startswith("| 時間"):
                return l
        return ""

    def auto_rows(self, txt):
        return [l for l in txt.splitlines() if "[auto]" in l]

    def test_new_calibration_log_has_model_column(self):
        tp = self.make_transcript("claude-sonnet-5")
        run_hook({"hook_event_name": "StopFailure", "error": "rate_limit", "transcript_path": tp},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        txt = self.log_text()
        self.assertIn("model", self.header_line(txt), "新檔表頭應含 model 欄，header=%r" % self.header_line(txt))
        row = self.auto_rows(txt)[-1]
        cells = [c.strip() for c in row.strip("|").split("|")]
        self.assertEqual(cells[-1], "claude-sonnet-5",
                          "新列最後一欄應是撞牆的 model，row=%r" % row)

    def test_row_records_unknown_model_when_no_transcript(self):
        run_hook({"hook_event_name": "StopFailure", "error": "rate_limit"},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        row = self.auto_rows(self.log_text())[-1]
        cells = [c.strip() for c in row.strip("|").split("|")]
        self.assertEqual(cells[-1], "unknown", "偵測失敗 model 欄應為 unknown，row=%r" % row)

    def test_existing_old_header_migrated_and_rows_preserved(self):
        # 預先寫一份「舊格式」calibration-log（表頭無 model 欄 + 一筆歷史資料列）
        old = (
            "# claude-hot-limit · 上限校準 log\n\n"
            "撞到 429/529 時由 StopFailure hook 自動記錄（[auto]）。\n\n"
            "## 觀測紀錄（trip 點）\n\n"
            "| 時間 | 訊號 / 註解 | 近60s | 近180s | 近300s | 近600s |\n"
            "|------|-------------|------:|-------:|-------:|-------:|\n"
            "| 2020-01-01 00:00:00 | [auto] rate_limit | 1 | 2 | 3 | 4 |\n"
        )
        with open(self.log, "w") as f:
            f.write(old)
        tp = self.make_transcript("claude-opus-4-8")
        run_hook({"hook_event_name": "StopFailure", "error": "rate_limit", "transcript_path": tp},
                 {"CLAUDE_HOT_LIMIT_DATA": self.data})
        txt = self.log_text()
        self.assertIn("model", self.header_line(txt), "既有表頭應被一次性遷移加上 model 欄")
        self.assertIn("| 2020-01-01 00:00:00 | [auto] rate_limit | 1 | 2 | 3 | 4 |", txt,
                      "歷史資料列必須原封不動保留")
        new_row = [l for l in self.auto_rows(txt) if "2020-01-01" not in l][-1]
        cells = [c.strip() for c in new_row.strip("|").split("|")]
        self.assertEqual(cells[-1], "claude-opus-4-8", "遷移後新列應帶 model，row=%r" % new_row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
