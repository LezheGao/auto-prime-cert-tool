#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automatic ECPP certificate generation and FactorDB upload tool.

Overview:
  - Fetches PRP (probable prime) numbers from FactorDB via JSON-RPC.
  - Generates primality certificates (ECPP) for each PRP using cypari / PARI-GP.
  - Uploads the certificates back to FactorDB (skipped when no token is given).
  - Runs a pool of worker processes, a fetcher thread, and a result collector.
  - Bilingual UI (Chinese / English).
  - All user-facing settings are supplied via command-line arguments.
"""

import argparse
import collections
import json
import logging
import multiprocessing as mp
import os
import queue
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

# Optional dependency: cypari (bindings to PARI/GP). Import failure is
# deferred until the config stage so the user gets a helpful message.
_CYPAI_IMPORT_ERROR = None
CYPAI_VERSION = "unknown"
try:
    import cypari
    from cypari import pari
    CYPAI_AVAILABLE = True
    CYPAI_VERSION = getattr(cypari, "__version__", "unknown")
except Exception as e:
    pari = None
    CYPAI_AVAILABLE = False
    _CYPAI_IMPORT_ERROR = f"{type(e).__name__}: {e}"


def _initial_log_level():
    """Read LOG_LEVEL env var and map to a logging level; default INFO."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_name, logging.INFO)


# Bootstrap the root logger based on LOG_LEVEL (INFO if unset/invalid).
logging.basicConfig(
    level=_initial_log_level(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Tool version / repository (used in User-Agent and certificate comments).
TOOL_VERSION = "1.1"
REPO_URL = "https://github.com/LezheGao/auto-prime-cert-tool"

# FactorDB JSON-RPC endpoint.
RPC_URL = "https://factordb.com/rpc"


def build_user_agent(email=None):
    """Build the HTTP User-Agent; mailto is included only when email is valid."""
    ua = f"auto_primecert_tool/v{TOOL_VERSION} ({REPO_URL})"
    if email:
        ua += f" (mailto:{email})"
    return ua


# Initial UA without mailto; overridden after CLI parsing.
USER_AGENT = build_user_agent()


# ==================== i18n / Localization ====================

_LANG = "zh"  # Current UI language: "zh" or "en".

# All localized strings. Each key maps to (Chinese text, English text).
MESSAGES = {
    "err_email_invalid": ("邮箱格式无效，已忽略（UA 中不含 mailto 字段）。",
                          "Invalid email format, ignored (no mailto in UA)."),
    "err_workers_positive": ("并发 worker 数必须 > 0", "Workers must be > 0"),
    "err_queue_positive": ("task_q 容量必须 > 0", "task_q capacity must be > 0"),
    "err_count_positive": ("任务数量必须 > 0", "Task count must be > 0"),
    "err_count_max": ("primo_batch 的 count 最大 32000", "primo_batch count max is 32000"),
    "err_cypari_missing": ("cypari 未安装或导入失败，请运行: pip install cypari",
                           "cypari not installed or import failed. Run: pip install cypari"),
    "err_cypari_init": ("cypari 初始化失败: {err}", "cypari init failed: {err}"),

    "workers_capped": ("workers 从 {orig} 下调为 {eff}（不超过任务数 {count}）",
                       "Workers reduced from {orig} to {eff} (no more than task count {count})"),

    "cfg_summary_title": ("===== 配置摘要 =====", "===== Config Summary ====="),
    "cfg_output_dir": ("输出目录", "Output dir"),
    "cfg_min_digits": ("PRP 最小位数", "Min PRP digits"),
    "cfg_parisizemax": ("parisizemax", "parisizemax"),
    "cfg_token": ("token", "token"),
    "cfg_token_none": ("无 token（跳过上传）", "no token (skip upload)"),
    "cfg_workers": ("并发 worker 进程数", "Worker processes"),
    "cfg_queue_size": ("task_q 容量", "task_q capacity"),
    "cfg_recent_maxlen": ("recent_done 容量", "recent_done capacity"),
    "cfg_recent_off": ("（关闭）", " (disabled)"),
    "cfg_max_retries": ("上传最大重试", "Max upload retries"),
    "cfg_base_backoff": ("退避基数", "Backoff base"),
    "cfg_loop_mode": ("loop 模式", "Loop mode"),
    "cfg_task_count": ("任务数", "Task count"),
    "cfg_count_inf": ("∞（持续运行）", "∞ (continuous)"),
    "cfg_log_level": ("日志级别", "Log level"),

    "up_read_fail": ("读取失败: {err}", "Read failed: {err}"),
    "up_not_dict": ("响应不是 dict: {res!r}", "Response not a dict: {res!r}"),
    "up_not_applied": ("服务器接受但未应用 (added=0): {resp}",
                       "Server accepted but not applied (added=0): {resp}"),
    "up_server_fail": ("服务器返回 ok={ok}: {resp}", "Server returned ok={ok}: {resp}"),
    "up_429_err": ("HTTP 429 after {n} retries", "HTTP 429 after {n} retries"),
    "up_http_err": ("HTTP {code}: {reason}", "HTTP {code}: {reason}"),
    "up_debug": ("  [UP] {name} 响应: {body}", "  [UP] {name} response: {body}"),
    "up_429_wait": ("  [429] {name} 限流，等 {wait:.1f}s ({attempt}/{max})",
                    "  [429] {name} rate limited, wait {wait:.1f}s ({attempt}/{max})"),

    "cert_write_failed": ("写证书文件失败: {err}", "Failed to write certificate: {err}"),
    "cert_too_short": ("证书太短（{n} 字节），疑似非证书: {sample!r}",
                       "Certificate too short ({n} bytes), likely invalid: {sample!r}"),
    "ecpp_generic_fail": ("ECPP 失败: {err}", "ECPP failed: {err}"),

    "fetch_resources": ("  [Fetcher] X-Fdb-Resources: {hdr}", "  [Fetcher] X-Fdb-Resources: {hdr}"),
    "fetch_no_prp": ("[Fetcher] 未获取到 PRP，结束预取", "[Fetcher] No PRP received, stop prefetching"),
    "fetch_5_empty": ("[Fetcher] 连续 5 次空响应，退出", "[Fetcher] 5 consecutive empty responses, exiting"),
    "fetch_empty_retry": ("[Fetcher] 空响应，10s 后重试 ({n}/5)", "[Fetcher] Empty response, retry in 10s ({n}/5)"),
    "fetch_all_dup": ("[Fetcher] 连续 {n} 次拉到的都是重复 fid，等 FactorDB 更新，{wait}s 后重试",
                      "[Fetcher] {n} consecutive duplicate fids, waiting FactorDB update, retry in {wait}s"),
    "fetch_single_no_new": ("[Fetcher] 单次模式：连续 {n} 次无新 fid，实际只入队 {total}/{target} 个，退出等待",
                            "[Fetcher] Single mode: {n} consecutive no-new-fid, enqueued {total}/{target}, exit"),
    "fetch_added": ("[Fetcher] 本轮入队 {added}（跳过 {skipped}）| 累计入队 {queued}，累计完成 {done}",
                    "[Fetcher] Enqueued {added} (skipped {skipped}) | queued {queued}, done {done}"),
    "fetch_error": ("[Fetcher] 获取失败: {err}", "[Fetcher] Fetch error: {err}"),
    "fetch_retry_5s": ("[Fetcher] 5s 后重试", "[Fetcher] Retry in 5s"),

    "pipeline_start": ("[Pipeline] 目标 {target}，worker 进程 {workers}，task_q 容量 {qs}，"
                       "recent_done 容量 {rm}，最小位数 {md}",
                       "[Pipeline] target {target}, worker processes {workers}, task_q cap {qs}, "
                       "recent_done cap {rm}, min digits {md}"),
    "pipeline_ctrl_c": ("[Pipeline] 收到 Ctrl+C，停止拉新任务，等当前 worker 完成...",
                        "[Pipeline] Ctrl+C received, stop fetching, waiting for current workers..."),
    "pipeline_force_kill": ("[Pipeline] Worker {wid}（PID {pid}）30s 内未退出，强制终止",
                            "[Pipeline] Worker {wid} (PID {pid}) did not exit in 30s, terminating"),
    "pipeline_worker_abnormal": ("[Pipeline] Worker {wid} 异常退出，退出码 {code}",
                                 "[Pipeline] Worker {wid} exited abnormally, code {code}"),

    "w_ecpp_start": ("[{tag}] ECPP {name} ({digits} 位)", "[{tag}] ECPP {name} ({digits} digits)"),
    "w_ecpp_ok": ("[{tag}] ECPP OK   {name}", "[{tag}] ECPP OK   {name}"),
    "w_ecpp_fail": ("[{tag}] ECPP FAIL {name} -> {err}", "[{tag}] ECPP FAIL {name} -> {err}"),
    "w_up_ok": ("[{tag}] UP   OK   {name}", "[{tag}] UP   OK   {name}"),
    "w_up_fail": ("[{tag}] UP   FAIL {name} -> {err}", "[{tag}] UP   FAIL {name} -> {err}"),

    "stats_title": ("===== 统计 =====", "===== Stats ====="),
    "stats_added": ("  累计入队任务   : {n}", "  Total enqueued  : {n}"),
    "stats_done": ("  累计完成任务   : {n}", "  Total completed : {n}"),
    "stats_skipped": ("  跳过重复 fid   : {n}", "  Skipped dups    : {n}"),
    "stats_pool": ("  在途任务       : {u}，recent_done={r}",
                   "  In-flight       : {u}, recent_done={r}"),
    "stats_gen_ok": ("  ECPP 成功      : {n}", "  ECPP OK         : {n}"),
    "stats_gen_fail": ("  ECPP 失败      : {n}", "  ECPP FAIL       : {n}"),
    "stats_up_ok": ("  上传成功       : {n}", "  Upload OK       : {n}"),
    "stats_up_fail": ("  上传失败       : {n}", "  Upload FAIL     : {n}"),
    "stats_failed_list": ("  失败清单（这些 .out 保留在输出目录）：",
                          "  Failed list (these .out kept in output dir):"),

    "loop_start": ("[Loop] 持续运行，长按 Ctrl+C 停止", "[Loop] Running, Ctrl+C to stop"),
    "loop_stopped": ("[Loop] 已停止。", "[Loop] Stopped."),
    "interrupt": ("[中断] 用户中止。", "[Interrupt] Aborted by user."),
}


def t(key, **kw):
    """Translate a message key for the current language; format with kwargs."""
    zh, en = MESSAGES.get(key, (key, key))
    text = zh if _LANG == "zh" else en
    return text.format(**kw) if kw else text


def set_language(lang_arg):
    """Determine UI language from CLI arg, then APP_LANG env var, then default zh."""
    global _LANG
    if lang_arg in ("zh", "en"):
        _LANG = lang_arg
        return
    env = os.environ.get("APP_LANG", "").strip().lower()
    if env in ("zh", "cn", "chinese", "中文"):
        _LANG = "zh"
        return
    if env in ("en", "english", "英文"):
        _LANG = "en"
        return
    _LANG = "zh"


# ==================== Command-line interface ====================

def parse_args(argv=None):
    """Parse command-line arguments (replaces the old interactive prompts)."""
    p = argparse.ArgumentParser(
        prog="autoprimecert",
        description="Automatic ECPP certificate generation and FactorDB upload tool.",
    )
    p.add_argument("--lang", choices=["zh", "en"], default=None,
                   help="UI language (default: env APP_LANG, else zh)")
    p.add_argument("--output-dir", default=os.path.join(os.getcwd(), "certs"),
                   help="Directory for generated .out certificates "
                        "(default: ./certs)")
    p.add_argument("--min-digits", type=int, default=1000,
                   help="Minimum PRP digit count (default: 1000)")
    p.add_argument("--parisizemax", type=int, default=1_000_000_000,
                   help="PARI parisizemax bytes per worker (default: 1e9)")
    p.add_argument("--token", default=os.environ.get("FACTORDB_TOKEN", ""),
                   help="FactorDB API token; empty or 'none' skips upload "
                        "(default: env FACTORDB_TOKEN)")
    p.add_argument("--email", default=None,
                   help="Contact email appended to User-Agent as mailto (optional)")
    p.add_argument("--workers", type=int, default=5,
                   help="Number of worker processes (default: 5; "
                        "capped at --count in finite mode)")
    p.add_argument("--max-retries", type=int, default=5,
                   help="Max upload retries on transient failures (default: 5)")
    p.add_argument("--base-backoff", type=float, default=2.0,
                   help="Exponential backoff base seconds (default: 2.0)")
    p.add_argument("--count", type=int, default=None,
                   help="Task count; omit for loop (infinite) mode")
    p.add_argument("--queue-size", type=int, default=None,
                   help="task_q capacity (default: scales with --workers)")
    p.add_argument("--recent-maxlen", type=int, default=200,
                   help="recent_done capacity; 0 disables (default: 200)")
    p.add_argument("--debug", action="store_true",
                   help="Enable DEBUG verbose logging")
    return p.parse_args(argv)


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def validate_email(s):
    """Return a trimmed email if it looks valid, else None (with a warning)."""
    if not s or s.lower() == "none":
        return None
    if _EMAIL_RE.match(s):
        return s
    log.warning(t("err_email_invalid"))
    return None


# ==================== FactorDB RPC / Upload ====================

def rpc_call(method, params, token=None, timeout=120):
    """Perform a single JSON-RPC call against the FactorDB endpoint.

    Returns (result, x_fdb_resources_header).
    Raises on RPC errors or non-JSON responses.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if token:
        headers["X-Fdb-User-Token"] = token
    req = urllib.request.Request(
        RPC_URL, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        res_header = resp.headers.get("X-Fdb-Resources")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"FactorDB returned non-JSON content: {raw[:300]}")
    if "error" in body:
        raise RuntimeError(f"RPC error: {body['error']}")
    return body["result"], res_header


def upload_one(path, token, max_retries, base_backoff):
    """Upload a single certificate file to FactorDB.

    Retries on HTTP 429 with exponential backoff; other HTTP errors are
    returned immediately. Returns (success: bool, message: str).
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            cert_text = f.read()
    except Exception as e:
        return False, t("up_read_fail", err=e)

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            res, _ = rpc_call(
                "upload_certificate",
                {"data": cert_text, "session": token},
                token=token, timeout=180,
            )
            try:
                resp_str = json.dumps(res, ensure_ascii=False)
            except Exception:
                resp_str = repr(res)
            log.debug(t("up_debug", name=os.path.basename(path), body=resp_str[:500]))

            if not isinstance(res, dict):
                return False, t("up_not_dict", res=res)
            ok = res.get("ok")
            added = res.get("added")
            if ok and added is not None and int(added) == 0:
                # Server accepted the upload but did not actually add a certificate.
                return False, t("up_not_applied", resp=resp_str[:400])
            if ok:
                return True, "ok"
            return False, t("up_server_fail", ok=ok, resp=resp_str[:400])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Rate limited: sleep with exponential backoff and retry.
                wait = base_backoff * (2 ** attempt)
                log.warning(t("up_429_wait", name=os.path.basename(path),
                              wait=wait, attempt=attempt + 1, max=max_retries))
                time.sleep(wait)
                last_err = t("up_429_err", n=max_retries)
                continue
            return False, t("up_http_err", code=e.code, reason=e.reason)
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries:
                time.sleep(base_backoff * (2 ** attempt))
                continue
            return False, last_err
    return False, last_err or "unknown error"


# ==================== cypari ECPP ====================

def _init_pari(parisizemax):
    """Configure PARI's parisizemax and silence memory-expansion warnings.

    Each spawned child process re-imports cypari and obtains its own PARI
    instance. Several fallback strategies are attempted in order.
    """
    for attempt in (
        # Preferred: disable debugmem + set parisizemax.
        lambda: (
            pari.default('debugmem', 0),
            pari.default('parisizemax', str(parisizemax)),
        ),
        # Fallback 1: set only parisizemax.
        lambda: pari.default('parisizemax', str(parisizemax)),
        # Fallback 2: allocate memory directly.
        lambda: pari.allocatemem(parisizemax),
    ):
        try:
            attempt()
            return
        except Exception:
            continue
    # If everything fails, keep defaults so startup is not blocked.


def ecpp_with_pari(n_decimal):
    """Compute and export an ECPP certificate string using module-level PARI.

    Prefers `primecert` (PARI >= 2.11), falling back to `ecpp`.
    Raises on any failure.
    """
    N = pari(n_decimal)

    # Prefer primecert (PARI >= 2.11) when available, otherwise fall back to ecpp.
    if hasattr(pari, "primecert"):
        try:
            C = pari.primecert(N)
            if pari.primecertisvalid(C):
                s = pari.primecertexport(C, 1)
                return str(s)
        except Exception as e:
            log.debug(f"primecert path failed: {e}, falling back to ecpp")

    # Fallback: legacy ecpp API.
    C = pari.ecpp(N)
    if not pari.ecppisvalid(C):
        raise RuntimeError("ecppisvalid failed")
    s = pari.ecppexport(C, 2)
    return str(s)


def _rewrite_cert_comments(cert_str):
    """Replace the [Comments] block of a certificate with our attribution.

    The trailing '\\n' after REPO_URL preserves the blank line that PARI
    emits between [Comments] and the next section (e.g. [Candidate]);
    dropping it would produce a non-conforming Primo .out file.

    If the certificate has no [Comments] section (e.g. produced by an
    unexpected export format), the input is returned unchanged.
    """
    new_comment = (
        f"Uploaded by auto primecert tool v{TOOL_VERSION} via cypari v{CYPAI_VERSION}\n"
        f"{REPO_URL}\n"
    )
    pat = re.compile(r"(\[Comments\]\n).*?(?=\n\[)", re.S)
    return pat.sub(lambda m: m.group(1) + new_comment, cert_str, count=1)


# ==================== Worker process ====================

def worker_main(wid, task_q, result_q, stop_event, cfg):
    """Entry point of a worker process.

    Pulls tasks from task_q, generates an ECPP certificate, writes it to
    disk, and uploads it to FactorDB when a token is configured. All
    progress is reported via result_q — the parent process owns logging.
    """
    global USER_AGENT
    USER_AGENT = build_user_agent(cfg.get('email'))

    try:
        # Let the parent handle Ctrl+C; workers shut down via sentinel/event.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass

    try:
        _init_pari(cfg['parisizemax'])
    except Exception as e:
        result_q.put({'type': 'worker_error', 'wid': wid,
                      'msg': t("err_cypari_init", err=e)})
        return

    output_dir = cfg['output_dir']
    token = cfg['token']
    max_retries = cfg['max_retries']
    base_backoff = cfg['base_backoff']

    while True:
        if stop_event.is_set():
            break
        try:
            # Short timeout so stop_event is observed even when idle.
            item = task_q.get(timeout=1.0)
        except queue.Empty:
            continue
        if item is None:  # Sentinel: graceful shutdown.
            break

        fid = item['fid']
        dec = str(int(item['hex'], 16))
        digits = item.get('digits', '?')
        out_path = os.path.join(output_dir, f'factordb_{fid}.out')
        name = os.path.basename(out_path)

        result_q.put({'type': 'ecpp_start', 'wid': wid, 'fid': fid,
                      'name': name, 'digits': digits})

        # Generate certificate and write it to disk.
        try:
            cert_str = ecpp_with_pari(dec)
            if cert_str:
                cert_str = _rewrite_cert_comments(cert_str)
            if not cert_str or len(cert_str) < 50:
                raise RuntimeError(t("cert_too_short",
                                     n=len(cert_str or ""),
                                     sample=(cert_str or "")[:120]))
            with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(cert_str.rstrip() + '\n')
        except Exception as e:
            result_q.put({'type': 'ecpp_fail', 'wid': wid, 'fid': fid,
                          'name': name, 'path': out_path,
                          'reason': str(e)[:1500]})
            result_q.put({'type': 'done', 'wid': wid, 'fid': fid, 'success': False})
            continue

        result_q.put({'type': 'ecpp_ok', 'wid': wid, 'fid': fid, 'name': name})

        # Skip upload when no token was provided.
        if not token:
            result_q.put({'type': 'done', 'wid': wid, 'fid': fid, 'success': False})
            continue

        try:
            up_ok, up_msg = upload_one(out_path, token, max_retries, base_backoff)
        except Exception as e:
            up_ok, up_msg = False, f'upload exception: {e}'

        if up_ok:
            # Remove the local certificate once successfully uploaded.
            try:
                os.remove(out_path)
            except OSError:
                pass
            result_q.put({'type': 'up_ok', 'wid': wid, 'fid': fid, 'name': name})
            result_q.put({'type': 'done', 'wid': wid, 'fid': fid, 'success': True})
        else:
            # Keep the .out file on failure so the user can retry manually.
            result_q.put({'type': 'up_fail', 'wid': wid, 'fid': fid,
                          'name': name, 'path': out_path, 'reason': up_msg})
            result_q.put({'type': 'done', 'wid': wid, 'fid': fid, 'success': False})


# ==================== Statistics ====================

class PipelineStats:
    """Thread-safe counters for pipeline progress and failed items.

    Two cumulative counters track different concepts and are deliberately
    kept separate so log lines can distinguish them:

      - added_total: tasks that have been put onto task_q (enqueued).
      - done_total:  tasks that have finished processing (worker reported
                     a 'done' event, regardless of success or failure).

    The difference (added_total - done_total) minus the current in-flight
    count is what's still sitting in the queue; in-flight is tracked
    externally by run_pipeline.
    """

    def __init__(self, target):
        self.target = target
        self.lock = threading.Lock()
        self.added_total = 0
        self.done_total = 0
        self.skipped_total = 0
        self.gen_ok = 0
        self.gen_fail = 0
        self.upload_ok = 0
        self.upload_fail = 0
        self.failed_paths = []

    def progress(self):
        """Atomically return (added_total, done_total, skipped_total)."""
        with self.lock:
            return self.added_total, self.done_total, self.skipped_total

    def record_added(self, n, skipped):
        """Record newly queued tasks and skipped duplicates; return running total."""
        with self.lock:
            self.added_total += n
            self.skipped_total += skipped
            return self.added_total

    def record_done(self):
        """Mark one task as finished (success or failure)."""
        with self.lock:
            self.done_total += 1

    def record_gen_ok(self):
        with self.lock:
            self.gen_ok += 1

    def record_gen_fail(self, path, reason):
        with self.lock:
            self.gen_fail += 1
            if path:
                self.failed_paths.append((path, f"ECPP: {reason}"))

    def record_upload_ok(self):
        with self.lock:
            self.upload_ok += 1

    def record_upload_fail(self, path, reason):
        with self.lock:
            self.upload_fail += 1
            if path:
                self.failed_paths.append((path, f"UP: {reason}"))

    def snapshot(self):
        """Return an atomic snapshot of all counters and failed items."""
        with self.lock:
            return (self.added_total, self.done_total, self.skipped_total,
                    self.gen_ok, self.gen_fail,
                    self.upload_ok, self.upload_fail,
                    list(self.failed_paths))


def print_stats(stats, in_flight_size, recent_size):
    """Log a human-readable summary of pipeline statistics."""
    (added, done, skipped, gen_ok, gen_fail,
     up_ok, up_fail, failed) = stats.snapshot()
    log.info(t("stats_title"))
    log.info(t("stats_added", n=added))
    log.info(t("stats_done", n=done))
    log.info(t("stats_skipped", n=skipped))
    log.info(t("stats_pool", u=in_flight_size, r=recent_size))
    log.info(t("stats_gen_ok", n=gen_ok))
    log.info(t("stats_gen_fail", n=gen_fail))
    log.info(t("stats_up_ok", n=up_ok))
    log.info(t("stats_up_fail", n=up_fail))
    if failed:
        log.info(t("stats_failed_list"))
        for p, reason in failed:
            log.info(f"    - {os.path.basename(p)}: {reason}")


# ==================== Result collector thread ====================

def result_collector(result_q, stop_event, stats, in_flight, in_flight_lock,
                     recent_done, n_workers, procs):
    """Main-process thread consuming worker events."""
    exited = 0
    while True:
        try:
            ev = result_q.get(timeout=0.5)
        except queue.Empty:
            workers_gone = all(not p.is_alive() for p in procs)
            if (exited >= n_workers and result_q.empty()) or (
                    stop_event.is_set() and workers_gone and result_q.empty()):
                time.sleep(0.2)
                if result_q.empty():
                    break
            continue

        typ = ev.get('type')
        wid = ev.get('wid')
        tag = f'W{wid}'
        name = ev.get('name', '')

        if typ == 'ecpp_start':
            log.info(t("w_ecpp_start", tag=tag, name=name,
                       digits=ev.get('digits', '?')))
        elif typ == 'ecpp_ok':
            stats.record_gen_ok()
            log.info(t("w_ecpp_ok", tag=tag, name=name))
        elif typ == 'ecpp_fail':
            stats.record_gen_fail(ev.get('path'), ev.get('reason', ''))
            log.error(t("w_ecpp_fail", tag=tag, name=name,
                        err=ev.get('reason', '')))
        elif typ == 'up_ok':
            stats.record_upload_ok()
            log.info(t("w_up_ok", tag=tag, name=name))
        elif typ == 'up_fail':
            stats.record_upload_fail(ev.get('path'), ev.get('reason', ''))
            log.error(t("w_up_fail", tag=tag, name=name,
                        err=ev.get('reason', '')))
        elif typ == 'done':
            # Task finished: remove from in_flight and remember successes
            # in recent_done to avoid re-requesting fids still cached by the server.
            with in_flight_lock:
                in_flight.discard(ev['fid'])
                if ev.get('success') and recent_done is not None:
                    recent_done.append(ev['fid'])
            # Count completion exactly once per task (success or failure).
            stats.record_done()
        elif typ == 'worker_exit':
            # (Reserved: workers do not emit this event in the current design.)
            exited += 1
        elif typ == 'worker_error':
            log.error(f"[W{wid}] {ev.get('msg')}")


# ==================== Pipeline orchestration ====================

def run_pipeline(cfg):
    """Run one full pipeline: spawn workers, fetch tasks, collect results."""
    output_dir = cfg['output_dir']
    os.makedirs(output_dir, exist_ok=True)
    target = cfg['count']
    n_workers = cfg['upload_workers']

    # Use "spawn" so each worker process gets its own PARI instance.
    ctx = mp.get_context('spawn')
    task_q = ctx.Queue(maxsize=cfg['queue_size'])
    result_q = ctx.Queue()
    stop_event = ctx.Event()

    stats = PipelineStats(target)
    in_flight = set()
    in_flight_lock = threading.Lock()
    recent_done = (collections.deque(maxlen=cfg['recent_maxlen'])
                   if cfg['recent_maxlen'] > 0 else None)

    target_disp = f"{target}" if target is not None else "∞"
    log.info(t("pipeline_start",
               target=target_disp, workers=n_workers,
               qs=cfg['queue_size'], rm=cfg['recent_maxlen'],
               md=cfg['min_digits']))

    # Spawn worker processes.
    procs = []
    for i in range(n_workers):
        p = ctx.Process(target=worker_main,
                        args=(i, task_q, result_q, stop_event, cfg),
                        name=f'worker-{i}')
        p.start()
        procs.append(p)

    # Start the collector thread before workers produce events.
    collector = threading.Thread(
        target=result_collector,
        args=(result_q, stop_event, stats, in_flight, in_flight_lock,
              recent_done, n_workers, procs),
        name='collector', daemon=True,
    )
    collector.start()

    def fetcher():
        """Fetch PRP tasks from FactorDB and enqueue them for workers."""
        consecutive_empty = 0
        consecutive_all_dup = 0
        try:
            while not stop_event.is_set():
                with in_flight_lock:
                    if len(in_flight) >= cfg['queue_size']:
                        # Queue is full; wait before polling again.
                        time.sleep(0.5)
                        continue
                    # Snapshot how many tasks have been enqueued so far.
                    current_added = stats.progress()[0]
                    if target is not None and current_added >= target:
                        break
                    need = cfg['queue_size'] - len(in_flight)
                    # In finite mode, never ask for more than what's left.
                    if target is not None:
                        need = min(need, target - current_added)

                # Request roughly twice the needed count, clamped to [8, 60].
                request_count = min(max(need * 2, 8), 60)
                try:
                    result, res_hdr = rpc_call(
                        "primo_batch",
                        {"digits": cfg['min_digits'], "count": request_count},
                        token=cfg['token'] or None,
                    )
                    if res_hdr:
                        log.debug(t("fetch_resources", hdr=res_hdr))
                    nums = result.get("numbers", [])
                    if not nums:
                        # No PRP returned by the server.
                        consecutive_empty += 1
                        if target is not None:
                            log.warning(t("fetch_no_prp"))
                            break
                        if consecutive_empty >= 5:
                            log.warning(t("fetch_5_empty"))
                            break
                        log.warning(t("fetch_empty_retry", n=consecutive_empty))
                        time.sleep(10)
                        continue
                    consecutive_empty = 0

                    added = skipped = 0
                    with in_flight_lock:
                        # Re-read the current total under the lock so the
                        # target boundary below is enforced per item, not
                        # just once per batch.
                        current_added = stats.progress()[0]
                        for item in nums:
                            fid = item['fid']
                            # Skip fids already in flight or recently completed.
                            if (fid in in_flight
                                    or (recent_done is not None and fid in recent_done)):
                                skipped += 1
                                continue
                            if len(in_flight) >= cfg['queue_size']:
                                break
                            # Do not exceed --count in finite mode.
                            if target is not None and current_added + added >= target:
                                break
                            try:
                                task_q.put_nowait(item)
                            except queue.Full:
                                break
                            in_flight.add(fid)
                            added += 1

                    # Update counters, then read the done total.
                    total_queued = stats.record_added(added, skipped)
                    _, total_done, _ = stats.progress()
                    if target is not None:
                        queued_str = f"{total_queued}/{target}"
                        done_str = f"{total_done}/{target}"
                    else:
                        queued_str = str(total_queued)
                        done_str = str(total_done)

                    if added == 0:
                        # Entire batch was duplicates; back off and retry.
                        consecutive_all_dup += 1
                        if consecutive_all_dup < 3:
                            wait = 5
                        elif consecutive_all_dup < 6:
                            wait = 15
                        else:
                            wait = 30
                        if consecutive_all_dup in (3, 6):
                            log.warning(t("fetch_all_dup",
                                          n=consecutive_all_dup, wait=wait))
                        if target is not None and consecutive_all_dup >= 10:
                            log.warning(t("fetch_single_no_new",
                                          n=consecutive_all_dup,
                                          total=total_queued, target=target))
                            break
                        time.sleep(wait)
                        continue

                    consecutive_all_dup = 0
                    log.info(t("fetch_added",
                               added=added, skipped=skipped,
                               queued=queued_str, done=done_str))
                except Exception as e:
                    log.error(t("fetch_error", err=e))
                    if target is not None and stats.progress()[0] == 0:
                        break
                    # Transient error; retry after a short delay.
                    log.warning(t("fetch_retry_5s"))
                    time.sleep(5)
        finally:
            # Send one sentinel per worker so each worker wakes up and exits.
            for _ in range(n_workers):
                try:
                    task_q.put(None, timeout=5)
                except queue.Full:
                    break

    fetcher_thread = threading.Thread(target=fetcher, name='fetcher', daemon=True)
    fetcher_thread.start()

    # Wait for all workers to exit normally, or handle Ctrl+C.
    abnormal = []
    try:
        for i, p in enumerate(procs):
            while p.is_alive():
                p.join(timeout=0.5)
            if p.exitcode not in (0, None):
                abnormal.append((i, p.exitcode))
    except KeyboardInterrupt:
        log.warning(t("pipeline_ctrl_c"))
        stop_event.set()
        # Nudge workers to exit via sentinels.
        for _ in range(n_workers):
            try:
                task_q.put_nowait(None)
            except queue.Full:
                pass
        # Wait for workers to finish their current task. 
        for i, p in enumerate(procs):
            p.join(timeout=30)
            if p.is_alive():
                # Force-terminate workers that refuse to exit in 30s.
                log.warning(t("pipeline_force_kill", wid=i, pid=p.pid))
                p.terminate()
                p.join(timeout=5)
        for wid, code in abnormal:
            log.warning(t("pipeline_worker_abnormal", wid=wid, code=code))
        time.sleep(0.5)
        collector.join(timeout=10)
        with in_flight_lock:
            print_stats(stats, len(in_flight), len(recent_done) if recent_done else 0)
        raise

    for wid, code in abnormal:
        log.warning(t("pipeline_worker_abnormal", wid=wid, code=code))

    # Normal termination: stop the collector and print final stats.
    stop_event.set()
    collector.join(timeout=10)
    with in_flight_lock:
        print_stats(stats, len(in_flight), len(recent_done) if recent_done else 0)
    return stats


# ==================== Config assembly & summary ====================

def collect_config(args):
    """Assemble the runtime configuration dict from parsed CLI arguments."""
    cfg = {}
    cfg['email'] = validate_email(args.email)
    cfg['output_dir'] = args.output_dir
    cfg['min_digits'] = args.min_digits
    cfg['parisizemax'] = args.parisizemax

    tok = (args.token or "").strip()
    if tok.lower() == "none":
        tok = ""
    cfg['token'] = tok

    cfg['upload_workers'] = args.workers
    cfg['max_retries'] = args.max_retries
    cfg['base_backoff'] = args.base_backoff
    cfg['count'] = args.count
    cfg['loop_mode'] = (args.count is None)

    # Validate workers early; later logic assumes a positive value.
    if cfg['upload_workers'] <= 0:
        sys.exit(t("err_workers_positive"))

    if cfg['count'] is not None:
        if cfg['count'] <= 0:
            sys.exit(t("err_count_positive"))
        if cfg['count'] > 32000:
            sys.exit(t("err_count_max"))
        # Cap workers to the task count: more workers than tasks is wasted
        # PARI instances (and memory) that would never receive work.
        if cfg['upload_workers'] > cfg['count']:
            log.info(t("workers_capped",
                       orig=cfg['upload_workers'],
                       eff=cfg['count'],
                       count=cfg['count']))
            cfg['upload_workers'] = cfg['count']

    if args.queue_size is not None:
        cfg['queue_size'] = args.queue_size
    else:
        if cfg['count'] is None:
            cfg['queue_size'] = max(cfg['upload_workers'] * 2, 4)
        else:
            cfg['queue_size'] = min(cfg['count'],
                                    max(cfg['upload_workers'] * 2, 4))

    cfg['recent_maxlen'] = args.recent_maxlen
    cfg['debug_log'] = args.debug
    logging.getLogger().setLevel(logging.DEBUG if cfg['debug_log'] else logging.INFO)

    if cfg['queue_size'] <= 0:
        sys.exit(t("err_queue_positive"))

    # Self-check the cypari environment before running the pipeline.
    if not CYPAI_AVAILABLE:
        msg = t("err_cypari_missing")
        if _CYPAI_IMPORT_ERROR:
            msg += f"\n  Error: {_CYPAI_IMPORT_ERROR}"
        msg += f"\n  Interpreter: {sys.executable}"
        sys.exit(msg)
    try:
        _ = pari(2).isprime()
    except Exception as e:
        sys.exit(t("err_cypari_init", err=e)
                 + f"\n  Interpreter: {sys.executable}")

    return cfg


def print_config_summary(cfg):
    """Log a human-readable summary of the chosen configuration."""
    log.info(t("cfg_summary_title"))
    log.info(f"  {t('cfg_output_dir')}: {cfg['output_dir']}")
    log.info(f"  {t('cfg_min_digits')}: {cfg['min_digits']}")
    log.info(f"  {t('cfg_parisizemax')}: {cfg['parisizemax']}")
    if not cfg['token']:
        tok_disp = t("cfg_token_none")
    else:
        tok_disp = f"{cfg['token'][:6]}...{cfg['token'][-4:]}"
    log.info(f"  {t('cfg_token')}: {tok_disp}")
    log.info(f"  {t('cfg_workers')}: {cfg['upload_workers']}")
    log.info(f"  {t('cfg_queue_size')}: {cfg['queue_size']}")
    log.info(f"  {t('cfg_recent_maxlen')}: {cfg['recent_maxlen']}"
             + (t("cfg_recent_off") if cfg['recent_maxlen'] == 0 else ""))
    log.info(f"  {t('cfg_max_retries')}: {cfg['max_retries']}")
    log.info(f"  {t('cfg_base_backoff')}: {cfg['base_backoff']} s")
    log.info(f"  {t('cfg_loop_mode')}: {cfg['loop_mode']}")
    cnt = t("cfg_count_inf") if cfg['count'] is None else cfg['count']
    log.info(f"  {t('cfg_task_count')}: {cnt}")
    log.info(f"  {t('cfg_log_level')}: {'DEBUG' if cfg['debug_log'] else 'INFO'}")
    log.info("====================")


# ==================== Main entry point ====================

def main(argv=None):
    """Program entry: parse CLI, print summary, then run the pipeline."""
    args = parse_args(argv)
    set_language(args.lang)
    cfg = collect_config(args)
    global USER_AGENT
    USER_AGENT = build_user_agent(cfg.get('email'))
    log.info(f"[UA] {USER_AGENT}")
    print_config_summary(cfg)

    if cfg['loop_mode']:
        log.info(t("loop_start"))
        try:
            run_pipeline(cfg)
        except KeyboardInterrupt:
            log.info(t("loop_stopped"))
    else:
        try:
            run_pipeline(cfg)
        except KeyboardInterrupt:
            log.info(t("interrupt"))


if __name__ == "__main__":
    # PyInstaller + multiprocessing spawn fix: on Windows, freeze_support()
    # intercepts the internal --multiprocessing-fork launch and dispatches
    # directly to spawn_main(), so the child never re-enters main()/argparse.
    # No-op when running from source.
    mp.freeze_support()
    main()
