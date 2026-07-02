#!/usr/bin/env python3
"""
claude-hot-limit · proxy-launcher 黑箱測試（#8 — Phase 1 proxy 部署）

launcher 當真實 subprocess 跑，驗證 opt-in gate / idempotent ensure / stop / fail-loud。
每個測試用自訂 free port + temp data dir（不碰真實 8787 / ~/.cache），tearDown 強制
stop 不留 orphan daemon。

跑法:
    python3 -m unittest discover -s tests
    python3 tests/test_proxy_launcher.py
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.join(os.path.dirname(HERE), "proxy", "proxy-launcher.py")


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_up(port, timeout=0.25):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


def run_launcher(subcmd, env_overrides, timeout=20):
    """跑 launcher subprocess；環境先剝掉宿主的相關變數再套 overrides（測試隔離）。"""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_HOT_LIMIT_")
           and not k.startswith("RATE_LIMIT_PROXY_")
           and k != "ANTHROPIC_BASE_URL"}
    env.update(env_overrides)
    proc = subprocess.run([sys.executable, LAUNCHER, subcmd],
                          capture_output=True, text=True, env=env, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


class ProxyLauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.port = free_port()
        self.env = {
            "CLAUDE_HOT_LIMIT_DATA": self.data,
            "RATE_LIMIT_PROXY_PORT": str(self.port),
        }

    def tearDown(self):
        run_launcher("stop", self.env)  # 強制回收 daemon，不留 orphan
        self.tmp.cleanup()

    def opted(self, **extra):
        e = dict(self.env, CLAUDE_HOT_LIMIT_PROXY="1")
        e.update(extra)
        return e

    def read_pid(self):
        p = os.path.join(self.data, "proxy.pid")
        return open(p).read().strip() if os.path.exists(p) else None

    def test_no_opt_in_is_silent_noop(self):
        # 無 CLAUDE_HOT_LIMIT_PROXY、無 ANTHROPIC_BASE_URL → ensure 靜默退出、不起 daemon
        code, out, err = run_launcher("ensure", self.env)
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "未 opt-in 應完全靜默，stdout=%r" % out)
        self.assertFalse(port_up(self.port), "未 opt-in 不該起 daemon")
        self.assertIsNone(self.read_pid(), "未 opt-in 不該寫 pidfile")

    def test_opt_in_flag_spawns_daemon(self):
        # CLAUDE_HOT_LIMIT_PROXY=1 強制 opt-in → daemon 起來、port 可連、pidfile 存在
        code, out, err = run_launcher("ensure", self.opted())
        self.assertEqual(code, 0, "ensure 應 exit 0，stderr=%r" % err)
        self.assertTrue(port_up(self.port), "opt-in 後 port 應可連")
        pid = self.read_pid()
        self.assertIsNotNone(pid, "應寫 pidfile")
        os.kill(int(pid), 0)  # process 活著（不拋 ProcessLookupError）

    def test_base_url_opt_in_spawns_daemon(self):
        # 主要 opt-in 訊號：ANTHROPIC_BASE_URL 指向本機 proxy port → 導流設定本身就是訊號
        env = dict(self.env, ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % self.port)
        code, out, err = run_launcher("ensure", env)
        self.assertEqual(code, 0)
        self.assertTrue(port_up(self.port),
                        "ANTHROPIC_BASE_URL 指向 proxy port 即 opt-in，應起 daemon")

    def test_ensure_is_idempotent(self):
        # 第二次 ensure 不該重複 spawn：pid 不變、port 持續 UP
        run_launcher("ensure", self.opted())
        pid1 = self.read_pid()
        code, out, err = run_launcher("ensure", self.opted())
        self.assertEqual(code, 0)
        self.assertEqual(self.read_pid(), pid1, "冪等：第二次 ensure 不該換 pid")
        self.assertTrue(port_up(self.port))

    def test_stop_cleans_up(self):
        run_launcher("ensure", self.opted())
        self.assertTrue(port_up(self.port))
        code, out, err = run_launcher("stop", self.env)
        self.assertEqual(code, 0)
        self.assertFalse(port_up(self.port), "stop 後 port 應關閉")
        self.assertIsNone(self.read_pid(), "stop 後 pidfile 應清除")

    def test_fail_loud_when_daemon_cannot_bind(self):
        # RATE_LIMIT_PROXY_PORT=1（privileged port，非 root bind 必敗）→ daemon 秒死。
        # fail-loud：stdout 必須有警告（SessionStart stdout 進 session context）+ 退回指引；
        # 但 exit 0 絕不擋 session。
        env = self.opted(RATE_LIMIT_PROXY_PORT="1")
        code, out, err = run_launcher("ensure", env)
        self.assertEqual(code, 0, "fail-loud 仍要 exit 0（不擋 session）")
        self.assertIn("啟動失敗", out, "應在 stdout 印啟動失敗警告，stdout=%r" % out)
        self.assertIn("ANTHROPIC_BASE_URL", out, "警告應含退回指引（提到導流 env），stdout=%r" % out)

    def test_disabled_kill_switch_wins_over_opt_in(self):
        # <data>/disabled 檔案旗標 → 即使 opt-in 也靜默 no-op（與 pacing-guard kill-switch 一致）
        open(os.path.join(self.data, "disabled"), "w").close()
        code, out, err = run_launcher("ensure", self.opted())
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "kill-switch 下應靜默，stdout=%r" % out)
        self.assertFalse(port_up(self.port), "kill-switch 下不該起 daemon")


if __name__ == "__main__":
    unittest.main(verbosity=2)
