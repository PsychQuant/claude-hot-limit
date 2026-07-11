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
    """跑 launcher subprocess；環境先剝掉宿主的相關變數再套 overrides（測試隔離）。
    subcmd 可為 str（單一子命令）或 list（子命令 + flags，如 ["stop", "--force"]，#27）。"""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_HOT_LIMIT_")
           and not k.startswith("RATE_LIMIT_PROXY_")
           and k != "ANTHROPIC_BASE_URL"}
    env.update(env_overrides)
    args = subcmd if isinstance(subcmd, list) else [subcmd]
    proc = subprocess.run([sys.executable, LAUNCHER] + args,
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
            # #27 verify F8：pin 低 DRAIN_CAP，避免 stop 內部窗（預設 125s）>
            # run_launcher 的 subprocess timeout（20s）——回歸時的失敗模式才會是
            # launcher 自己的有界 escalation，而非 TimeoutExpired + 孤兒 daemon。
            "RATE_LIMIT_PROXY_DRAIN_CAP": "2",
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
        # <data>/disabled 檔案旗標 → 即使 opt-in（無導流 env）也靜默 no-op（與 pacing-guard 一致）。
        # 注意：這裡只設 CLAUDE_HOT_LIMIT_PROXY=1、沒設 ANTHROPIC_BASE_URL——沒有導流就沒有
        # dead-port 風險，kill-switch 保持完全靜默是對的。有導流的警告見下一個測試。
        open(os.path.join(self.data, "disabled"), "w").close()
        code, out, err = run_launcher("ensure", self.opted())
        self.assertEqual(code, 0)
        self.assertEqual(out, "", "kill-switch 下（無導流）應靜默，stdout=%r" % out)
        self.assertFalse(port_up(self.port), "kill-switch 下不該起 daemon")

    # --- verify-fix（#8 verify findings 2/3/5/17：靜默 dead-port cluster 必須 fail-loud）---

    def test_kill_switch_with_routing_warns_dead_port(self):
        # [3][5] kill-switch 先於 opt-in 的靜默退出：若使用者導流 env 還在、port 又沒人聽，
        # 「停用」等於靜默把全部 API 流量斷掉 → 必須警告（仍 exit 0、仍不起 daemon）。
        open(os.path.join(self.data, "disabled"), "w").close()
        env = dict(self.env, ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % self.port)
        code, out, err = run_launcher("ensure", env)
        self.assertEqual(code, 0)
        self.assertFalse(port_up(self.port), "kill-switch 下仍不該起 daemon")
        self.assertIn("ANTHROPIC_BASE_URL", out,
                      "kill-switch + 導流 + dead port → 必須警告，stdout=%r" % out)

    def test_port_mismatch_warns_when_target_down(self):
        # [2] URL 指向本機「另一個」port 且該 port 沒人聽：launcher 不管它（非 opt-in），
        # 但這正是文件說的頭號風險情境 → 不可靜默，要提示 RATE_LIMIT_PROXY_PORT 對齊。
        other = free_port()
        env = dict(self.env, ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % other)
        code, out, err = run_launcher("ensure", env)
        self.assertEqual(code, 0)
        self.assertFalse(port_up(self.port), "mismatch 下不該起 launcher 管理的 daemon")
        self.assertFalse(port_up(other), "mismatch 下也不該去佔別人的 port")
        self.assertIn("RATE_LIMIT_PROXY_PORT", out,
                      "port mismatch + target down → 必須提示對齊方式，stdout=%r" % out)

    def test_https_scheme_warns_but_daemon_starts(self):
        # [17] https:// 指向 plaintext proxy：gate 會過、daemon 健康、但 TLS handshake 必敗
        # → daemon 照起（port/host 正確），另外警告 scheme。
        env = dict(self.env, ANTHROPIC_BASE_URL="https://127.0.0.1:%d" % self.port)
        code, out, err = run_launcher("ensure", env)
        self.assertEqual(code, 0)
        self.assertTrue(port_up(self.port), "host/port 正確，daemon 應照起")
        self.assertIn("http://", out, "https scheme → 必須警告改用 http://，stdout=%r" % out)

    def test_stop_does_not_kill_foreign_pid(self):
        # [10][11] pidfile 的 pid 被 reuse 成無關 process → stop 不可誤殺；清 pidfile + 警告。
        victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with open(os.path.join(self.data, "proxy.pid"), "w") as f:
                f.write(str(victim.pid))
            code, out, err = run_launcher("stop", self.env)
            self.assertEqual(code, 0)
            self.assertIsNone(victim.poll(), "stop 誤殺了非 proxy 的 process（PID reuse 情境）")
            self.assertIsNone(self.read_pid(), "stale pidfile 應被清除")
        finally:
            victim.kill()
            victim.wait()


# --- #27 graceful stop / restart ---

# 假 daemon：cmdline 內含 "rate-limit-proxy" 字串以通過 stop() 的身分驗證。
FAKE_SLOW_EXIT = (
    "# rate-limit-proxy test-fake (slow exit)\n"
    "import signal, sys, time\n"
    "flag = []\n"
    "signal.signal(signal.SIGTERM, lambda *a: flag.append(time.time()))\n"
    "t0 = time.time()\n"
    "while time.time() - t0 < 30:\n"
    "    time.sleep(0.1)\n"
    "    if flag and time.time() - flag[0] >= 2.0:\n"
    "        sys.exit(0)\n"
)
FAKE_IGNORE_SIGTERM = (
    "# rate-limit-proxy test-fake (ignores SIGTERM)\n"
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "time.sleep(60)\n"
)


class GracefulStopTest(unittest.TestCase):
    """#27 — stop 預設 graceful（等 daemon 真的死 + SIGKILL fallback）、--force 逃生、restart。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.port = free_port()
        self.env = {
            "CLAUDE_HOT_LIMIT_DATA": self.data,
            "RATE_LIMIT_PROXY_PORT": str(self.port),
            # #27 verify F8：pin 低 DRAIN_CAP，避免 stop 內部窗（預設 125s）>
            # run_launcher 的 subprocess timeout（20s）——回歸時的失敗模式才會是
            # launcher 自己的有界 escalation，而非 TimeoutExpired + 孤兒 daemon。
            "RATE_LIMIT_PROXY_DRAIN_CAP": "2",
        }
        self.fake = None

    def tearDown(self):
        if self.fake is not None and self.fake.poll() is None:
            self.fake.kill()
            self.fake.wait()
        run_launcher("stop", self.env)
        self.tmp.cleanup()

    def opted(self, **extra):
        e = dict(self.env, CLAUDE_HOT_LIMIT_PROXY="1")
        e.update(extra)
        return e

    def read_pid(self):
        p = os.path.join(self.data, "proxy.pid")
        return open(p).read().strip() if os.path.exists(p) else None

    def _spawn_fake(self, code):
        self.fake = subprocess.Popen([sys.executable, "-c", code])
        with open(os.path.join(self.data, "proxy.pid"), "w") as f:
            f.write(str(self.fake.pid))
        return self.fake

    def test_stop_waits_until_daemon_actually_dead(self):
        fake = self._spawn_fake(FAKE_SLOW_EXIT)  # SIGTERM 後 ~2s 才退（模擬 drain 中）
        t0 = time.time()
        code, out, err = run_launcher("stop", dict(self.env, RATE_LIMIT_PROXY_DRAIN_CAP="4"))
        elapsed = time.time() - t0
        self.assertEqual(code, 0)
        self.assertIsNotNone(fake.poll(),
                             "stop 返回時 daemon 必須已死（現行 2s port-only 等待會提早返回）")
        self.assertIsNone(self.read_pid(), "pidfile 應在確認死亡後清除")
        # #27 verify F3：fake 是本測試 process 的 child，退出後成 zombie——
        # 壞掉的 _pid_alive（kill(pid,0) 對 zombie 成功）會等滿 4+5s 窗 + 假 SIGKILL。
        # 及時偵測死亡 = elapsed 貼近 fake 的 ~2s 實際退出時間。
        self.assertLess(elapsed, 6,
                        "死亡偵測應及時（殭屍盲點會等滿 9s 窗），實測 %.1fs" % elapsed)
        self.assertNotIn("SIGKILL", out + err,
                         "乾淨退出不該觸發 SIGKILL fallback（假警告 = 殭屍誤判）")

    def test_stop_escalates_sigkill_on_stuck_daemon(self):
        fake = self._spawn_fake(FAKE_IGNORE_SIGTERM)  # SIGTERM 無效 → 必須 SIGKILL fallback
        t0 = time.time()
        code, out, err = run_launcher("stop", dict(self.env, RATE_LIMIT_PROXY_DRAIN_CAP="1"),
                                      timeout=30)
        elapsed = time.time() - t0
        self.assertEqual(code, 0)
        time.sleep(0.3)  # SIGKILL 生效時間
        self.assertIsNotNone(fake.poll(), "卡死 daemon 應被 SIGKILL fallback 收掉")
        self.assertLess(elapsed, 20, "等待窗應有界（cap+5+margin）")
        self.assertIn("SIGKILL", out + err, "escalation 應有可見警告")

    def test_stop_force_kills_immediately(self):
        fake = self._spawn_fake(FAKE_IGNORE_SIGTERM)
        t0 = time.time()
        code, out, err = run_launcher(["stop", "--force"], self.env)
        elapsed = time.time() - t0
        self.assertEqual(code, 0)
        time.sleep(0.3)
        self.assertIsNotNone(fake.poll(), "--force 應立即收掉 daemon")
        self.assertLess(elapsed, 5, "--force 不等 drain")

    def test_restart_exits_nonzero_when_new_daemon_fails_to_start(self):
        # #27 re-verify round-3（DA Attack 5，HIGH）：ensure() 的「永遠 exit 0」契約
        # （SessionStart 不擋 session，正確）不可洩漏進 restart——restart 的契約是
        # 「結束時有 daemon 在 port 上」，新 daemon 起不來（此處用 privileged port 1
        # 模擬 bind 失敗）就必須非零 exit，否則部署腳本看 exit code 會被騙。
        env = self.opted(RATE_LIMIT_PROXY_PORT="1")
        code, out, err = run_launcher("restart", env, timeout=30)
        self.assertNotEqual(code, 0,
                            "新 daemon 啟動失敗時 restart 不得回 0（silent dead port）")

    def test_restart_replaces_daemon(self):
        code, out, err = run_launcher("ensure", self.opted())
        self.assertEqual(code, 0)
        self.assertTrue(port_up(self.port), "前置：ensure 起 daemon")
        old_pid = self.read_pid()
        self.assertIsNotNone(old_pid)

        code, out, err = run_launcher("restart", self.opted(), timeout=30)
        self.assertEqual(code, 0, "restart 應是合法子命令（現行 usage exit 2）: %s" % err)
        self.assertTrue(port_up(self.port), "restart 後 port 應回到 UP")
        new_pid = self.read_pid()
        self.assertIsNotNone(new_pid)
        self.assertNotEqual(new_pid, old_pid, "restart 應是新 process")
        try:
            os.kill(int(old_pid), 0)
            self.fail("舊 daemon 應已死")
        except ProcessLookupError:
            pass


class LogRotationTest(unittest.TestCase):
    """#17 — proxy.log spawn-time rotation（只留一代 .1；log 無語料價值）。

    ensure() 在開 proxy.log append 之前（daemon 未跑；two-phase restart 下舊 daemon
    drain 期間仍可能持 fd——其輸出跟去 .1，已披露）size 檢查：
    > RATE_LIMIT_PROXY_LOG_ROTATE_MB（float MiB=1024²，預設 32）→ os.replace 成 .1。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.port = free_port()
        self.env = {
            "CLAUDE_HOT_LIMIT_DATA": self.data,
            "RATE_LIMIT_PROXY_PORT": str(self.port),
            "RATE_LIMIT_PROXY_DRAIN_CAP": "2",
            "CLAUDE_HOT_LIMIT_PROXY": "1",
        }
        self.log = os.path.join(self.data, "proxy.log")

    def tearDown(self):
        run_launcher("stop", self.env)
        self.tmp.cleanup()

    def test_oversized_log_rotated_to_dot1_on_spawn(self):
        with open(self.log, "w") as f:
            f.write("OLD-DAEMON-LOG " * 20)  # ~300 bytes
        env = dict(self.env, RATE_LIMIT_PROXY_LOG_ROTATE_MB="0.0001")  # cap ~105B
        code, out, err = run_launcher("ensure", env)
        self.assertEqual(code, 0, "ensure 應成功，stderr=%r" % err)
        self.assertTrue(os.path.exists(self.log + ".1"),
                        "超 cap 的 proxy.log 應在 spawn 前輪替成 .1")
        with open(self.log + ".1") as f:
            self.assertIn("OLD-DAEMON-LOG", f.read())

    def test_existing_dot1_is_replaced_single_generation(self):
        with open(self.log + ".1", "w") as f:
            f.write("GEN-MINUS-2")
        with open(self.log, "w") as f:
            f.write("GEN-MINUS-1 " * 30)
        env = dict(self.env, RATE_LIMIT_PROXY_LOG_ROTATE_MB="0.0001")
        code, out, err = run_launcher("ensure", env)
        self.assertEqual(code, 0)
        with open(self.log + ".1") as f:
            content = f.read()
        self.assertIn("GEN-MINUS-1", content, ".1 應被最新一代覆蓋")
        self.assertNotIn("GEN-MINUS-2", content, "只留一代——更舊的內容應被丟棄")

    def test_below_cap_not_rotated(self):
        with open(self.log, "w") as f:
            f.write("small log\n")
        code, out, err = run_launcher("ensure", self.env)  # 預設 cap 32MiB
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.log + ".1"), "未達 cap 不該輪替")

    def test_bad_log_cap_values_do_not_break_ensure(self):
        # verify F1（R2+Codex，launcher 側）：「1e308」有限巨值 ×1024² 溢位 →
        # 未捕捉 OverflowError 會讓 ensure 整個崩、daemon 不 spawn（dead-port 級）。
        # 壞值只能退回預設，絕不能擋 spawn。verify F4（R3）：launcher 原本零壞值測試。
        for bad in ("1e308", "abc", "nan", "0"):
            with self.subTest(cap=bad):
                run_launcher("stop", self.env)  # 每輪乾淨起點
                env = dict(self.env, RATE_LIMIT_PROXY_LOG_ROTATE_MB=bad)
                code, out, err = run_launcher("ensure", env)
                self.assertEqual(code, 0,
                                 "cap=%r 不得讓 ensure 失敗，stderr=%r" % (bad, err))
                self.assertTrue(port_up(self.port),
                                "cap=%r 不得擋 daemon spawn（dead-port 級後果）" % bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
