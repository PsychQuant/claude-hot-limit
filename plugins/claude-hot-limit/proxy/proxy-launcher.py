#!/usr/bin/env python3
"""
claude-hot-limit · proxy-launcher  (#8 — Phase 1 proxy 部署)

idempotent 啟動器：把 rate-limit-proxy.py 以 detached daemon 起在本機（預設 port 8787），
供 SessionStart hook 每個 session 自動 ensure、或手動呼叫。

用法:
    python3 proxy-launcher.py ensure          # opt-in 且 port 沒人聽 → 起 daemon（冪等）
    python3 proxy-launcher.py stop            # graceful：SIGTERM + 等 drain 完（超時 SIGKILL fallback）
    python3 proxy-launcher.py stop --force    # 立即 SIGKILL（接受切斷並發 streams）
    python3 proxy-launcher.py restart         # 兩階段：port 釋放即起新 daemon，舊的背景 drain（#27）
    python3 proxy-launcher.py status          # 顯示 port / pidfile 狀態

Opt-in gate（ensure 的動作條件，缺一即靜默退出 exit 0）：
  - kill-switch 未開（CLAUDE_HOT_LIMIT_OFF != 1 且 <data>/disabled 不存在）
  - ANTHROPIC_BASE_URL 指向 127.0.0.1/localhost 的 RATE_LIMIT_PROXY_PORT（預設 8787），
    或 CLAUDE_HOT_LIMIT_PROXY=1 強制。
  → **導流設定本身就是 opt-in 訊號**：沒設導流的使用者永遠不會被起一個用不到的
    daemon；設了導流的自動獲得「daemon 在第一個 API 請求前就緒」的保障。

fail-loud：opt-in 成立但 daemon 起不來（bind 失敗等）→ stdout 印警告（SessionStart
hook 的 stdout 會進 session context，使用者一定看得到）+ 退回指引，exit 0 絕不擋 session。

為什麼是 Python 不是 bash：macOS 沒有 flock(1)，並發 session 的 ensure 序列化只能用
fcntl.flock——與本 repo 既有 hook 同款；且 daemon 本身就是 Python，零新增依賴。
Windows 無 fcntl → 跳過鎖（fail-open，沿用 rate-limit-proxy.py 同款 fallback）。
"""
import os
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows：跳過鎖，頂多並發 ensure 重複探測

DEFAULT_PORT = 8787
_HEALTH_WAIT_SECS = 3.0  # daemon 起來到 port 可連的等待上限（SessionStart 延遲的天花板）


def data_dir():
    return os.environ.get("CLAUDE_HOT_LIMIT_DATA") or os.path.expanduser("~/.cache/claude-hot-limit")


def proxy_port():
    try:
        return int(os.environ.get("RATE_LIMIT_PROXY_PORT", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def port_up(port, timeout=0.25):
    """TCP connect 探測：有人在聽就視為 UP（不區分是不是我們的 proxy——fail-open
    傾向：port 被佔就絕不重複 spawn；pid 不符由 status 顯示警告）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except Exception:
        return False


def kill_switch_on(d):
    return os.environ.get("CLAUDE_HOT_LIMIT_OFF") == "1" or os.path.exists(os.path.join(d, "disabled"))


def _local_base_url():
    """解析 ANTHROPIC_BASE_URL；指向本機（127.0.0.1/localhost）→ (scheme, host, port)，否則 None。

    這是「使用者已把流量導到本機某 port」的事實訊號——不論那個 port 是不是我們管的。
    verify findings 2/3/5：所有「導流存在但 launcher 決定不服務」的分支都必須據此 fail-loud，
    不能靜默留下 dead-port（= 全流量斷，部署層頭號風險）。解析失敗 → None（保守）。"""
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if not base:
        return None
    try:
        u = urlparse(base)
        host = (u.hostname or "").lower()
        if host in ("127.0.0.1", "localhost"):
            return (u.scheme or "http", host, u.port)
    except Exception:
        pass
    return None


def opted_in(port):
    """opt-in 訊號：ANTHROPIC_BASE_URL 指向本機 proxy port（主要路徑——導流設定即訊號），
    或 CLAUDE_HOT_LIMIT_PROXY=1 強制（測試 / 手動預熱用）。"""
    if os.environ.get("CLAUDE_HOT_LIMIT_PROXY") == "1":
        return True
    base = _local_base_url()
    return base is not None and base[2] == port


def _pid_path(d):
    return os.path.join(d, "proxy.pid")


def _read_pid(d):
    try:
        return int(open(_pid_path(d)).read().strip())
    except Exception:
        return None


def _fail_loud(port, log_path):
    """起不來時的警告——這是部署層的頭號風險（dead-port = 全流量斷），必須大聲。"""
    print(
        "[claude-hot-limit] ⚠️ rate-limit-proxy 啟動失敗（port {port}）。"
        "若 ANTHROPIC_BASE_URL 指向此 proxy，所有 API 請求將無法送出！"
        "檢查 {log}；緊急退回：從 ~/.claude/settings.json 的 env 移除 ANTHROPIC_BASE_URL"
        "（或 unset CLAUDE_HOT_LIMIT_PROXY）後重啟 session。".format(port=port, log=log_path))


def ensure():
    d = data_dir()
    port = proxy_port()
    base = _local_base_url()
    if kill_switch_on(d):
        # kill-switch 優先於 opt-in（與 pacing-guard 的 disabled 語意一致）——但若使用者的
        # 導流 env 還指著一個沒人聽的本機 port，「停用」等於靜默斷掉全部 API 流量
        # （verify findings 3/5：kill-switch 的爆炸半徑從「關掉保護」變成「斷流量」）→ 警告。
        if base is not None and base[2] is not None and not port_up(base[2]):
            print(
                "[claude-hot-limit] ⚠️ kill-switch（CLAUDE_HOT_LIMIT_OFF / disabled 檔）生效，"
                "proxy daemon 不會啟動——但 ANTHROPIC_BASE_URL 仍指向本機 port {p} 且該 port "
                "無人服務，所有 API 請求將無法送出。退回：移除 settings.json env 的 "
                "ANTHROPIC_BASE_URL，或解除 kill-switch 後重啟 session。".format(p=base[2]))
        return 0
    if not opted_in(port):
        # 未 opt-in：對沒導流的使用者完全靜默。但「導流指向本機另一個 port 且那個 port 沒人聽」
        # 是文件標明的頭號風險情境（verify finding 2：port mismatch 靜默 dead-port）→ 提示對齊。
        if (base is not None and base[2] is not None
                and base[2] != port and not port_up(base[2])):
            print(
                "[claude-hot-limit] ⚠️ ANTHROPIC_BASE_URL 指向本機 port {b}，但 launcher 管理的是 "
                "RATE_LIMIT_PROXY_PORT={p}（不一致，daemon 不會啟動），且 port {b} 目前無人服務"
                "——所有 API 請求將無法送出。若要用本 plugin 的 proxy：設 RATE_LIMIT_PROXY_PORT={b} "
                "或把 URL 改成 port {p}。若 port {b} 是其他工具的 gateway，請確認它有啟動"
                "（此警告可忽略）。".format(b=base[2], p=port))
        return 0
    if base is not None and base[0] == "https":
        # verify finding 17：https:// 指向 plaintext proxy——gate 會過、daemon 健康，
        # 但 Claude Code 對它做 TLS handshake 必敗。daemon 照起（host/port 正確），警告 scheme。
        print(
            "[claude-hot-limit] ⚠️ ANTHROPIC_BASE_URL 用 https:// 指向本機 proxy，但 proxy 是 "
            "plaintext HTTP——TLS handshake 會失敗，所有請求無法送出。請改成 "
            "http://127.0.0.1:{p}。".format(p=port))
    if port_up(port):
        return 0  # 已有 daemon（本 session 或別的 session 起的）→ 冪等 no-op

    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    log_path = os.path.join(d, "proxy.log")
    lockf = None
    try:
        if fcntl is not None:
            try:
                lockf = open(os.path.join(d, "proxy-launcher.lock"), "a")
                fcntl.flock(lockf, fcntl.LOCK_EX)
            except Exception:
                lockf = None  # 鎖壞掉 fail-open：頂多並發重複探測，二次 port 檢查兜底
        if port_up(port):
            return 0  # 鎖內二次探測：並發 session 在我們等鎖時已把 daemon 起好

        proxy_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rate-limit-proxy.py")
        try:
            logf = open(log_path, "a")
        except Exception:
            logf = subprocess.DEVNULL
        proc = subprocess.Popen(
            [sys.executable, proxy_py],
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            start_new_session=True)  # detached：session 結束 daemon 續活（帳號級共用）
        if logf is not subprocess.DEVNULL:
            logf.close()  # Popen 已 dup fd，父行程可關
        try:
            with open(_pid_path(d), "w") as f:
                f.write(str(proc.pid))
        except Exception:
            pass

        deadline = time.time() + _HEALTH_WAIT_SECS
        while time.time() < deadline:
            if port_up(port):
                return 0
            if proc.poll() is not None:
                break  # daemon 已死（bind 失敗等），不必等滿
            time.sleep(0.1)
        _fail_loud(port, log_path)
        return 0  # fail-loud 但絕不非零 exit：不能因 launcher 失敗擋 session
    except Exception as e:
        print("[claude-hot-limit] ⚠️ rate-limit-proxy 啟動失敗（launcher 異常: {e}）。"
              "若 ANTHROPIC_BASE_URL 指向此 proxy，所有 API 請求將無法送出！"
              "緊急退回：移除該 env 後重啟 session。".format(e=e))
        return 0
    finally:
        if lockf is not None:
            try:
                fcntl.flock(lockf, fcntl.LOCK_UN)
                lockf.close()
            except Exception:
                pass


def _pid_command(pid):
    """回傳 pid 的 command line（`ps -o command=`），查不到 / 平台無 ps → None（unknown）。"""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def _pid_alive(pid):
    """process 是否真的活著。#27 verify F3：`os.kill(pid, 0)` 對 zombie（已退出、
    未被 reap）也成功——等待迴圈會被殭屍卡滿整個窗 + 觸發假 SIGKILL。ps stat 以
    Z 開頭視同死亡；ps 不可用（degraded 平台）→ 沿用 kill(0) 判定。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # 存在但無權 signal——視為活著，交由身分檢查/上層處理
    except Exception:
        return False
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if not out or out.startswith("Z"):
            return False
    except Exception:
        pass
    return True


def _still_ours(pid):
    """kill 前的身分重查（#27 verify F4：TOCTOU——長等待窗內 PID reuse 會讓 SIGKILL
    誤殺無辜 process）。查不到 command（degraded 平台）→ 沿用「照殺」的既有取捨。"""
    cmd = _pid_command(pid)
    return cmd is None or "rate-limit-proxy" in cmd


def _drain_cap():
    """與 rate-limit-proxy 的 RATE_LIMIT_PROXY_DRAIN_CAP 共用同一 env（#27）；
    壞值（含 inf/nan/負值，verify F7）回 120——「有界」是硬承諾。"""
    try:
        v = float(os.environ.get("RATE_LIMIT_PROXY_DRAIN_CAP", "120"))
        return v if (0 <= v < float("inf")) else 120.0
    except (ValueError, TypeError):
        return 120.0


def stop(force=False):
    """停 pidfile 記錄的 daemon 並清 pidfile。冪等：無 pidfile / process 已死都安靜成功。

    #27 graceful 語意（預設）：SIGTERM 後**等到 process 真的死**（daemon 端會 drain
    in-flight streams，等待窗 = DRAIN_CAP + 5s，每 2s 印進度）；超時 → SIGKILL fallback
    + 警告。`--force` → 立即 SIGKILL（逃生路徑，接受切斷並發 streams）。
    pidfile 在**確認死亡後**才清（先刪會讓中途失敗留下無主 daemon）。

    身分驗證（verify findings 10/11）：pidfile 可能因 daemon 死亡 + OS PID reuse 而指向無關
    process——kill 前先看 command line 含 rate-limit-proxy 才殺；確認是別人 → 不殺、警告、
    只清 stale pidfile。查不到 command（unknown，如無 ps 的平台）→ 沿用舊行為照殺（保守的
    另一面：讓 stop 在 degraded 平台仍能運作）。"""
    d = data_dir()
    port = proxy_port()
    pid = _read_pid(d)
    killed_ok = True
    if pid is not None:
        cmd = _pid_command(pid)
        if cmd is not None and "rate-limit-proxy" not in cmd:
            print("[claude-hot-limit] ⚠️ pidfile 的 pid {pid} 不是 rate-limit-proxy"
                  "（command: {cmd}；PID reuse？）——不 kill，僅清除 stale pidfile。".format(
                      pid=pid, cmd=cmd[:120]))
        elif force:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and _pid_alive(pid):
                time.sleep(0.05)
            killed_ok = not _pid_alive(pid)
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.monotonic() + _drain_cap() + 5.0
            next_progress = time.monotonic() + 2.0
            while time.monotonic() < deadline and _pid_alive(pid):
                if time.monotonic() >= next_progress:
                    print("[claude-hot-limit] graceful stop：等待 daemon drain in-flight "
                          "streams…（pid {p}）".format(p=pid))
                    next_progress = time.monotonic() + 2.0
                time.sleep(0.1)
            if _pid_alive(pid):
                # verify F4：長等待窗後 kill 前重查身分（PID reuse 防誤殺）
                if not _still_ours(pid):
                    print("[claude-hot-limit] ⚠️ 等待期間 pid {p} 已換人（PID reuse）"
                          "——不 SIGKILL，僅清 pidfile。".format(p=pid))
                else:
                    print("[claude-hot-limit] ⚠️ daemon 未在 drain 窗內退出 → SIGKILL fallback"
                          "（pid {p}）".format(p=pid))
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline and _pid_alive(pid):
                        time.sleep(0.05)
                    killed_ok = not _pid_alive(pid)
        # verify F9：pid 已死但 port 仍被佔——外部 listener 會讓 ensure/restart 誤判
        if not _pid_alive(pid) and port_up(port):
            print("[claude-hot-limit] ⚠️ pid {p} 已死但 port {pt} 仍被佔用——"
                  "可能有外部 process 佔 port，ensure/restart 會誤以為 daemon 健在。".format(
                      p=pid, pt=port))
    if not killed_ok:
        # verify F5：未確認死亡 → 保留 pidfile + 回非零（restart 的 gate 因此是活的）
        print("[claude-hot-limit] ⚠️ 未能終止 daemon（pid {p}）——保留 pidfile，exit 1。".format(p=pid))
        return 1
    try:
        os.remove(_pid_path(d))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return 0


def restart():
    """兩階段 graceful restart（#27；verify F6 修正版）。

    dead-port 窗真正最小化：daemon 收 SIGTERM 後 `server_close()` 幾乎立即釋放 port
    （drain 只擋 process 退出、不擋 port）——所以 **port 一釋放就 spawn 新 daemon**
    （Phase 1），舊 process 的 drain 在新 daemon 已上線後於背景等完（Phase 2）。
    等舊 process「全死」才 ensure 的舊寫法，在有真流量時 dead-port 窗 ≈ 整段 drain。
    ensure 的 opt-in gate 照舊——未 opt-in 時 ensure 靜默 no-op。"""
    d = data_dir()
    port = proxy_port()
    pid = _read_pid(d)
    old_ours = False
    if pid is not None:
        cmd = _pid_command(pid)
        if cmd is not None and "rate-limit-proxy" not in cmd:
            print("[claude-hot-limit] ⚠️ pidfile 的 pid {p} 不是 rate-limit-proxy——"
                  "視為 stale，僅清除。".format(p=pid))
            pid = None
        else:
            old_ours = True
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    # Phase 1：等 port 釋放（正常 < 1s：daemon 收 SIGTERM 後 server_close 幾乎立即）
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and port_up(port):
        time.sleep(0.1)
    if port_up(port):
        # re-verify (c)：Phase 1 已送過 SIGTERM 且等了 15s——再跑完整 stop() 會重複
        # SIGTERM + 再等一輪 ~125s（最壞 140s dead port）。卡死 daemon 直接升級 SIGKILL
        #（身分重查防 PID reuse）；SIGKILL 後 port 仍被佔 = 外部 process 佔 port，
        # 誠實 fail（exit 1）而非靜默 no-op。
        print("[claude-hot-limit] ⚠️ port {pt} 未在 15s 內釋放 → 直接升級 SIGKILL。".format(pt=port))
        if pid is not None and _pid_alive(pid):
            if _still_ours(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and _pid_alive(pid):
                    time.sleep(0.05)
            else:
                print("[claude-hot-limit] ⚠️ pid {p} 已換人（PID reuse）——不 SIGKILL。".format(p=pid))
        if port_up(port):
            print("[claude-hot-limit] ⚠️ SIGKILL 後 port {pt} 仍被佔用——外部 process 佔 port？"
                  "無法 restart，exit 1。".format(pt=port))
            return 1
    # 舊 pidfile 先清（ensure spawn 會寫新 daemon 的 pidfile）
    try:
        os.remove(_pid_path(d))
    except Exception:
        pass
    rc = ensure()
    # Phase 2：新 daemon 已上線；背景等舊 process drain 完（不動新 pidfile），
    # 超時且身分重查仍是我們的 → SIGKILL。
    if old_ours and pid is not None:
        deadline = time.monotonic() + _drain_cap() + 5.0
        next_progress = time.monotonic() + 2.0
        while time.monotonic() < deadline and _pid_alive(pid):
            if time.monotonic() >= next_progress:
                print("[claude-hot-limit] restart：新 daemon 已上線，等舊 daemon"
                      "（pid {p}）drain…".format(p=pid))
                next_progress = time.monotonic() + 2.0
            time.sleep(0.1)
        if _pid_alive(pid):
            if _still_ours(pid):
                print("[claude-hot-limit] ⚠️ 舊 daemon 未在 drain 窗內退出 → SIGKILL（pid {p}）。".format(p=pid))
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            else:
                print("[claude-hot-limit] ⚠️ 等待期間 pid {p} 已換人（PID reuse）——不 SIGKILL。".format(p=pid))
    return rc


def status():
    d = data_dir()
    port = proxy_port()
    pid = _read_pid(d)
    up = port_up(port)
    alive = False
    if pid is not None:
        try:
            os.kill(pid, 0)
            alive = True
        except Exception:
            alive = False
    print("port %d: %s; pidfile: %s%s" % (
        port, "UP" if up else "down",
        pid if pid is not None else "(none)",
        "" if (pid is None or alive) else " (dead)"))
    if up and pid is not None and not alive:
        print("⚠️ port UP 但 pidfile 的 process 已死——port 可能被其他 process 佔用")
    return 0


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ensure"
    flags = sys.argv[2:]
    if cmd == "ensure":
        sys.exit(ensure())
    if cmd == "stop":
        sys.exit(stop(force=("--force" in flags)))
    if cmd == "restart":
        sys.exit(restart())
    if cmd == "status":
        sys.exit(status())
    print("usage: proxy-launcher.py {ensure|stop [--force]|restart|status}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
