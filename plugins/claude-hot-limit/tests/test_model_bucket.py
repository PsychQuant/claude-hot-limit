#!/usr/bin/env python3
"""
claude-hot-limit · model_bucket() 單元測試（#6）

model_bucket() 是純函式，直接 import 測（不像其他 test 走黑箱 subprocess）。
模組名含連字號，用 importlib 載入。

跑法:
    python3 -m unittest discover -s tests
    python3 tests/test_model_bucket.py
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "pacing-guard.py")

_spec = importlib.util.spec_from_file_location("pacing_guard", HOOK)
pacing_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pacing_guard)
model_bucket = pacing_guard.model_bucket


class ModelBucketTest(unittest.TestCase):
    def test_opus_variants_share_bucket(self):
        self.assertEqual(model_bucket("claude-opus-4-8"), "opus-4")
        self.assertEqual(model_bucket("claude-opus-4-7"), "opus-4")
        self.assertEqual(model_bucket("claude-opus-4-8"), model_bucket("claude-opus-4-7"))

    def test_sonnet_4_variants_share_bucket(self):
        # 核心：4-5 與 4-6 是同一個 Sonnet 4.x 桶
        self.assertEqual(model_bucket("claude-sonnet-4-5"), "sonnet-4")
        self.assertEqual(model_bucket("claude-sonnet-4-6"), "sonnet-4")
        self.assertEqual(model_bucket("claude-sonnet-4-5"), model_bucket("claude-sonnet-4-6"))

    def test_sonnet_5_is_separate_bucket(self):
        self.assertEqual(model_bucket("claude-sonnet-5"), "sonnet-5")
        self.assertNotEqual(model_bucket("claude-sonnet-5"), model_bucket("claude-sonnet-4-5"))

    def test_haiku_bucket_and_date_suffix_ignored(self):
        self.assertEqual(model_bucket("claude-haiku-4-5"), "haiku-4")
        self.assertEqual(model_bucket("claude-haiku-4-5-20251001"), "haiku-4")

    def test_case_insensitive(self):
        self.assertEqual(model_bucket("Claude-Sonnet-4-5"), "sonnet-4")

    def test_none_and_unknown_pass_through(self):
        # unscoped 語意的載體：None / "unknown" 原樣回傳，呼叫端的 `x not in (None,"unknown")` 判斷才成立
        self.assertIsNone(model_bucket(None))
        self.assertEqual(model_bucket("unknown"), "unknown")

    def test_unrecognized_id_falls_through_to_itself(self):
        # 保守 fall-through：舊命名 scheme / 非 Anthropic 字串 → 回自身，只與自己相等，絕不 over-merge
        self.assertEqual(model_bucket("claude-3-5-sonnet-20241022"), "claude-3-5-sonnet-20241022")
        self.assertEqual(model_bucket("gpt-5.5"), "gpt-5.5")
        self.assertEqual(model_bucket(""), "")
        # 兩個不同的未知 id 不得被合併
        self.assertNotEqual(model_bucket("claude-3-5-sonnet-20241022"),
                            model_bucket("claude-3-opus-20240229"))


if __name__ == "__main__":
    unittest.main()
