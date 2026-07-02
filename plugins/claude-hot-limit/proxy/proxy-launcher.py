#!/usr/bin/env python3
"""
claude-hot-limit · proxy-launcher  (#8 — Phase 1 proxy 部署)

idempotent 啟動器：把 rate-limit-proxy.py 以 detached daemon 起在本機（預設 port 8787），
供 SessionStart hook 每個 session 自動 ensure、或手動呼叫。

用法:
    python3 proxy-launcher.py ensure   # opt-in 且 port 沒人聽 → 起 daemon（冪等）
    python3 proxy-launcher.py stop     # SIGTERM pidfile 記錄的 daemon 並清理
    python3 proxy-launcher.py status   # 顯示 port / pidfile 狀態

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


def opted_in(port):
    """opt-in 訊號：ANTHROPIC_BASE_URL 指向本機 proxy port（主要路徑——導流設定即訊號），
    或 CLAUDE_HOT_LIMIT_PROXY=1 強制（測試 / 手動預熱用）。"""
    if os.environ.get("CLAUDE_HOT_LIMIT_PROXY") == "1":
        return True
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    if not base:
        return False
    try:
        u = urlparse(base)
        host = (u.hostname or "").lower()
        return host in ("127.0.0.1", "localhost") and u.port == port
    except Exception:
        return False  # 解析不了的 URL 不當 opt-in（保守；絕不因壞 env 起 daemon）


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
    if kill_switch_on(d):
        return 0  # kill-switch 優先於 opt-in（與 pacing-guard 的 disabled 語意一致）
    if not opted_in(port):
        return 0  # 未 opt-in：完全靜默，絕不打擾沒用 proxy 的使用者
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


def stop():
    """SIGTERM pidfile 記錄的 daemon 並清 pidfile。冪等：無 pidfile / process 已死都安靜成功。"""
    d = data_dir()
    port = proxy_port()
    pid = _read_pid(d)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.time() + 2.0
        while time.time() < deadline and port_up(port):
            time.sleep(0.05)
    try:
        os.remove(_pid_path(d))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return 0


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
    if cmd == "ensure":
        sys.exit(ensure())
    if cmd == "stop":
        sys.exit(stop())
    if cmd == "status":
        sys.exit(status())
    print("usage: proxy-launcher.py {ensure|stop|status}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
