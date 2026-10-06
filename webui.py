#!/usr/bin/env python3
"""mikan-gopeed 本地面板：查看下载状态与更新记录，手动触发 RSS 刷新。

仅监听 127.0.0.1，供本机浏览器使用。安全设计：
- Host 头校验（防 DNS rebinding）
- POST /api/refresh 必须携带启动时生成的随机令牌（防 CSRF）
- 响应白名单：不返回 token / rss_url；日志输出前脱敏
- 全部接口固定路径，无请求可控的文件/URL 参数
- 页面零外部资源（无 CDN/字体/统计外呼）
"""

import contextlib
import io
import json
import os
import secrets
import struct
import subprocess
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mikan_gopeed as service

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = Path.home() / "Library" / "Logs" / "mikan-gopeed.log"
TOKEN = secrets.token_urlsafe(24)

refresh_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "summary": None,
    "error": None,
    "output": "",
}
_refresh_mutex = threading.Lock()

# ── 配置与路径 ───────────────────────────────────────────


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def service_paths(config):
    def _p(key, default):
        value = os.path.expanduser(config.get(key, str(BASE_DIR / default)))
        return value if os.path.isabs(value) else str(BASE_DIR / value)

    return {
        "record": Path(_p("record_path", "番剧更新记录.md")),
        "backup": Path(_p("record_backup_path", "record-backup.md")),
        "pending": Path(_p("pending_path", "record-pending.json")),
        "state": Path(_p("state_path", "state.json")),
        "icon_source": Path(_p("webui_icon", "icon.png")),
    }


# ── 可测试的纯函数：记录解析 / 日志状态 ──────────────────


def parse_record_md(text):
    """解析番剧更新记录 md → [{time, title, rss, files}]（新→旧）。"""
    entries = []
    for block in text.split("\n## ")[1:]:
        lines = block.split("\n")
        when = lines[0].strip()
        title, rss, files = "", "", []
        for line in lines[1:]:
            s = line.strip()
            if s.startswith("- **") and s.endswith("**"):
                title = s[4:-2]
            elif s.startswith("- RSS 标题："):
                rss = s[len("- RSS 标题："):].strip()
            elif s.startswith("- 文件：`") and s.endswith("`"):
                files.append(s[len("- 文件：`"):-1])
        entries.append({"time": when, "title": title, "rss": rss, "files": files})
    return entries


def parse_log_status(lines):
    """解析服务日志尾部 → {"summary": {...}|None, "errors": [...], "warnings": [...]}。

    规则：最后一个 SUMMARY 行代表最近一轮；该轮的所有 ERROR 行都在它之前。
    """
    summary = None
    summary_idx = -1
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].startswith("SUMMARY "):
            try:
                summary = json.loads(lines[idx][len("SUMMARY "):])
                summary_idx = idx
            except Exception:
                pass
            break
    chunk_start = 0
    for idx in range(summary_idx - 1, -1, -1):
        if lines[idx].startswith("SUMMARY "):
            chunk_start = idx + 1
            break
    chunk = lines[chunk_start: summary_idx if summary_idx >= 0 else len(lines)]
    errors = [line for line in chunk if line.startswith("ERROR")]
    warnings = [
        line for line in chunk
        if line.startswith(("GUARD", "AMBIGUOUS", "TASK ERROR")) or "not reachable" in line
    ]
    return {"summary": summary, "errors": errors, "warnings": warnings}


# ── 占位图标（纯标准库 PNG 生成） ─────────────────────────


def generate_placeholder_png(size=180):
    """深色圆角方块 + 白色播放三角的占位图标。"""
    radius = size * 0.225
    cx = cy = size / 2
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")  # filter: none
        for x in range(size):
            dx = max(radius - x, x - (size - 1 - radius), 0)
            dy = max(radius - y, y - (size - 1 - radius), 0)
            if dx * dx + dy * dy > radius * radius:
                row += b"\x00\x00\x00\x00"
                continue
            tx = x - cx + size * 0.05
            ty = y - cy
            in_triangle = (
                -size * 0.14 <= ty <= size * 0.15
                and tx >= -size * 0.11
                and ty <= (tx + size * 0.11) * (size * 0.15 / (size * 0.11 + size * 0.16))
                and ty >= -(tx + size * 0.11) * (size * 0.15 / (size * 0.11 + size * 0.16))
            )
            if in_triangle:
                row += b"\xff\xff\xff\xff"
            else:
                row += b"\x1d\x1d\x1f\xff"
        rows.append(bytes(row))
    raw = b"".join(rows)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ensure_icons(config):
    """源图存在且比缓存新时，用 sips 生成两档尺寸的 PNG 图标。"""
    paths = service_paths(config)
    src = paths["icon_source"]
    if not src.exists():
        return
    at = BASE_DIR / "apple-touch-icon.png"
    fv = BASE_DIR / "favicon.png"
    src_mtime = src.stat().st_mtime
    for target, side in ((at, 180), (fv, 32)):
        if not target.exists() or target.stat().st_mtime < src_mtime:
            subprocess.run(
                ["sips", "-s", "format", "png", "-z", str(side), str(side),
                 str(src), "--out", str(target)],
                check=False, capture_output=True,
            )


def icon_bytes(name):
    target = BASE_DIR / name
    if target.exists():
        return target.read_bytes()
    return _placeholder_cache()


_placeholder_lock = threading.Lock()
_placeholder_cache_value = None


def _placeholder_cache():
    global _placeholder_cache_value
    with _placeholder_lock:
        if _placeholder_cache_value is None:
            _placeholder_cache_value = generate_placeholder_png(180)
        return _placeholder_cache_value


# ── 状态聚合 ─────────────────────────────────────────────


def tail_log_lines(limit=80):
    if not LOG_PATH.exists():
        return [], None
    text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    lines = [service.sanitize_token(line) for line in text.splitlines() if line.strip()]
    return lines[-limit:], LOG_PATH.stat().st_mtime


def make_client(config):
    cfg = config.get("gopeed_api", {})
    return service.GopeedClient(
        cfg.get("base_url", "http://127.0.0.1:9999"),
        cfg.get("token", ""),
        cfg.get("unix_socket", ""),
        start_app=False,
    )


def collect_status(config):
    paths = service_paths(config)
    state = service.load_state(paths["state"])
    pending = service.load_pending(paths["pending"])

    gopeed = {"ok": False, "version": None}
    try:
        info = make_client(config).info(timeout=4)
        gopeed = {"ok": True, "version": (info or {}).get("version")}
    except Exception:
        pass

    lines, log_mtime = tail_log_lines()
    cycle = parse_log_status(lines)
    rss_error = next((e for e in cycle["errors"] if "RSS" in e), None)

    record = {"ok": True, "error": None}
    records = []
    try:
        if paths["record"].exists():
            records = parse_record_md(paths["record"].read_text(encoding="utf-8"))
        else:
            record = {"ok": False, "error": "记录文件尚未生成"}
    except Exception as exc:
        record = {"ok": False, "error": str(exc)}

    return {
        "version": service.__version__,
        "gopeed": gopeed,
        "rss": {"ok": rss_error is None, "error": rss_error},
        "record": record,
        "seen": len(state.get("seen", {})),
        "pending": len(pending),
        "last_cycle": {
            "log_time": log_mtime,
            "summary": cycle["summary"],
            "errors": cycle["errors"][-3:],
            "warnings": cycle["warnings"][-3:],
        },
        "refresh": {
            k: refresh_state[k] for k in
            ("running", "started_at", "finished_at", "ok", "summary", "error")
        },
        "auto_interval": int(config.get("poll_interval_seconds", 600)),
    }


# ── 手动刷新 ─────────────────────────────────────────────


def start_refresh():
    if refresh_state["running"]:
        return False, "已有刷新在运行中，请稍候"
    if not _refresh_mutex.acquire(blocking=False):
        return False, "已有刷新在运行中，请稍候"

    def worker():
        try:
            refresh_state.update(
                running=True, started_at=time.time(), finished_at=None,
                ok=None, summary=None, error=None, output="",
            )
            config = load_config()
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                rc = service.run_once(config, dry_run=False, verbose=True)
            output = buffer.getvalue()
            summary = None
            for line in output.splitlines():
                if line.startswith("SUMMARY "):
                    try:
                        summary = json.loads(line[len("SUMMARY "):])
                    except Exception:
                        pass
            refresh_state.update(
                running=False, finished_at=time.time(), ok=(rc == 0),
                summary=summary, error=None, output=output[-4000:],
            )
        except Exception as exc:
            refresh_state.update(
                running=False, finished_at=time.time(), ok=False, error=str(exc)
            )
        finally:
            service.release_run_lock()
            _refresh_mutex.release()

    threading.Thread(target=worker, daemon=True).start()
    return True, "已开始刷新"


# ── 页面 ─────────────────────────────────────────────────

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>番剧面板</title>
<link rel="icon" href="/favicon.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
:root{
  --bg:#f5f5f7; --card:#ffffff; --text:#1d1d1f; --muted:#86868b;
  --accent:#0071e3; --ok:#34c759; --bad:#ff3b30; --warn:#ff9f0a;
  --radius:18px; --shadow:0 2px 12px rgba(0,0,0,.06);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Helvetica Neue",sans-serif;
  -webkit-font-smoothing:antialiased;min-height:100vh}
.nav{position:sticky;top:0;z-index:10;background:rgba(245,245,247,.8);
  backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px);
  border-bottom:1px solid rgba(0,0,0,.06)}
.nav-inner{max-width:980px;margin:0 auto;display:flex;align-items:center;
  justify-content:space-between;padding:14px 22px}
.nav-title{font-size:17px;font-weight:600}
.button{background:var(--accent);color:#fff;border:none;border-radius:980px;
  padding:8px 20px;font-size:14px;font-weight:500;cursor:pointer;
  font-family:inherit;transition:transform .15s ease,opacity .2s ease}
.button:hover{opacity:.92}.button:active{transform:scale(.97)}
.button:disabled{opacity:.45;cursor:default}
.wrap{max-width:980px;margin:0 auto;padding:32px 22px 64px}
.hero h1{font-size:40px;font-weight:700;letter-spacing:-.6px}
.hero p{color:var(--muted);font-size:16px;margin-top:8px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-top:28px}
.card{background:var(--card);border-radius:var(--radius);padding:20px;
  box-shadow:var(--shadow)}
.stat .v{font-size:26px;font-weight:700;display:flex;align-items:center;gap:8px}
.stat .l{color:var(--muted);font-size:12px;margin-top:6px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;flex:none}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
.dot.warn{background:var(--warn)}.dot.idle{background:var(--muted)}
section{margin-top:40px}
h2{font-size:22px;font-weight:700;margin-bottom:14px;letter-spacing:-.3px}
.record{background:var(--card);border-radius:var(--radius);padding:16px 20px;
  margin-bottom:12px;box-shadow:var(--shadow)}
.record .time{color:var(--muted);font-size:12px;margin-bottom:4px}
.record .t{font-weight:600;font-size:15px}
.record .sub{color:var(--muted);font-size:12px;margin-top:6px;
  word-break:break-all;line-height:1.6}
.mono{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:11px}
.logbox{background:#1d1d1f;color:#f5f5f7;border-radius:var(--radius);
  padding:16px 18px;font-family:ui-monospace,"SF Mono",Menlo,monospace;
  font-size:11px;line-height:1.7;white-space:pre-wrap;word-break:break-all;
  max-height:320px;overflow:auto}
.empty{color:var(--muted);font-size:14px;padding:24px;text-align:center;
  background:var(--card);border-radius:var(--radius);box-shadow:var(--shadow)}
.warnline{color:var(--warn)}.errline{color:var(--bad)}
footer{max-width:980px;margin:48px auto 0;padding:0 22px 32px;
  color:var(--muted);font-size:12px}
.spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.4);
  border-top-color:#fff;border-radius:50%;animation:s .8s linear infinite;
  vertical-align:-2px;margin-right:6px}
@keyframes s{to{transform:rotate(360deg)}}
@media(max-width:720px){.grid{grid-template-columns:repeat(2,1fr)}
  .hero h1{font-size:30px}}
</style>
</head>
<body>
<div class="nav"><div class="nav-inner">
  <div class="nav-title">番剧面板</div>
  <button class="button" id="refreshBtn" onclick="doRefresh()">立即刷新 RSS</button>
</div></div>
<div class="wrap">
  <div class="hero">
    <h1>番剧更新</h1>
    <p>mikan-gopeed v@@VERSION@@ · 自动下载 · 每一集都有记录</p>
  </div>
  <div class="grid">
    <div class="card stat"><div class="v" id="stGopeed">—</div><div class="l">Gopeed 下载器</div></div>
    <div class="card stat"><div class="v" id="stRss">—</div><div class="l">RSS 订阅</div></div>
    <div class="card stat"><div class="v" id="stSeen">—</div><div class="l">去重记录</div></div>
    <div class="card stat"><div class="v" id="stPending">—</div><div class="l">下载中任务</div></div>
  </div>
  <section>
    <h2>更新记录</h2>
    <div id="records"></div>
  </section>
  <section>
    <h2>手动刷新</h2>
    <div class="record" id="refreshBox">尚未手动刷新过</div>
  </section>
  <section>
    <h2>最近自动巡检</h2>
    <div class="record" id="cycleBox">加载中…</div>
    <details style="margin-top:12px">
      <summary style="cursor:pointer;color:var(--muted);font-size:13px">查看日志尾部</summary>
      <div class="logbox" id="logBox" style="margin-top:12px">…</div>
    </details>
  </section>
</div>
<footer>仅本机可访问 · <span id="footVer"></span></footer>
<script>
const TOKEN = "@@TOKEN@@";
const $ = (id) => document.getElementById(id);
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = "HTTP " + r.status;
    try { msg = (await r.json()).error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}
function esc(s){ const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
function dot(ok, warn){ return '<span class="dot ' + (ok ? "ok" : (warn ? "warn" : "bad")) + '"></span>'; }

function renderStatus(s) {
  const g = $("stGopeed");
  g.innerHTML = dot(s.gopeed.ok) + esc(s.gopeed.ok ? "运行中" : "未运行");
  const r = $("stRss");
  if (s.rss.ok) { r.innerHTML = dot(true) + "正常"; }
  else { r.innerHTML = dot(false) + "失败"; r.title = s.rss.error || ""; }
  $("stSeen").textContent = s.seen + " 条";
  $("stPending").textContent = s.pending + " 个";
  $("footVer").textContent = "mikan-gopeed v" + s.version + " · 面板仅本机可访问";

  const c = s.last_cycle || {};
  const sum = c.summary;
  let cyc = "";
  if (sum) {
    cyc = "最近一轮：新条目 " + sum.items + " · 下载 " + sum.created +
          " · 跳过 " + sum.skipped + " · 失败 " + sum.errors;
    if (c.errors && c.errors.length) {
      cyc += "<br>" + c.errors.map(e => '<span class="errline">' + esc(e) + "</span>").join("<br>");
    }
    if (c.warnings && c.warnings.length) {
      cyc += "<br>" + c.warnings.map(e => '<span class="warnline">' + esc(e) + "</span>").join("<br>");
    }
  } else { cyc = "暂无巡检记录"; }
  $("cycleBox").innerHTML = cyc;
  $("logBox").textContent = s.log_tail || "…";

  const rb = $("refreshBtn");
  const rf = s.refresh || {};
  if (rf.running) {
    rb.disabled = true;
    rb.innerHTML = '<span class="spin"></span>刷新中…';
    $("refreshBox").innerHTML = '<span class="spin" style="border-color:rgba(0,113,227,.3);border-top-color:var(--accent)"></span> 正在拉取 RSS 并处理新条目…（大文件下载完成后会立即写入记录）';
  } else {
    rb.disabled = false;
    rb.textContent = "立即刷新 RSS";
    let txt = "尚未手动刷新过";
    if (rf.finished_at) {
      const t = new Date(rf.finished_at * 1000).toLocaleString("zh-CN", {hour12: false});
      const sm = rf.summary;
      txt = "上次手动刷新：" + t + (rf.ok ? " ✓" : " ✗") +
            (sm ? "（新条目 " + sm.items + " · 下载 " + sm.created + " · 跳过 " + sm.skipped + " · 失败 " + sm.errors + "）" : "") +
            (rf.error ? " 错误：" + rf.error : "");
    }
    $("refreshBox").innerHTML = esc(txt);
  }
}

async function loadStatus() {
  try { renderStatus(await api("/api/status")); }
  catch (e) {
    $("stGopeed").innerHTML = dot(false) + "离线";
    $("refreshBox").innerHTML = '面板连接失败：' + esc(e.message);
  }
}

async function loadRecords() {
  try {
    const list = await api("/api/records");
    if (!list.length) { $("records").innerHTML = '<div class="empty">还没有更新记录</div>'; return; }
    $("records").innerHTML = list.map(it => {
      let html = '<div class="record"><div class="time">' + esc(it.time) + '</div>' +
                 '<div class="t">' + esc(it.title) + "</div>";
      if (it.rss) html += '<div class="sub">RSS：' + esc(it.rss) + "</div>";
      for (const f of (it.files || [])) html += '<div class="sub mono">文件：' + esc(f) + "</div>";
      return html + "</div>";
    }).join("");
  } catch (e) {
    $("records").innerHTML = '<div class="empty">记录读取失败：' + esc(e.message) + "</div>";
  }
}

let refreshing = false;
async function doRefresh() {
  if (refreshing) return;
  refreshing = true;
  try { await api("/api/refresh", {method: "POST", headers: {"X-Panel-Token": TOKEN}}); }
  catch (e) { refreshing = false; await loadStatus(); return; }
  const timer = setInterval(async () => {
    const s = await api("/api/status");
    renderStatus(s);
    if (!s.refresh.running) {
      clearInterval(timer);
      refreshing = false;
      await loadRecords();
      await loadStatus();
    }
  }, 2000);
  await loadStatus();
}

setInterval(loadStatus, 5000);
loadStatus();
loadRecords();
</script>
</body>
</html>
"""

# ── HTTP 服务 ────────────────────────────────────────────

ALLOWED_HOSTS = None  # 启动时按端口填充


class PanelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mikan-gopeed-panel"

    def _host_ok(self):
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if not self._host_ok():
            self._send(403, b'{"error":"forbidden host"}')
            return
        path = self.path.split("?")[0]
        config = load_config()
        if path == "/":
            page = (
                PAGE.replace("@@TOKEN@@", TOKEN)
                .replace("@@VERSION@@", service.__version__)
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(collect_status(config))
        elif path == "/api/records":
            paths = service_paths(config)
            try:
                text = (
                    paths["record"].read_text(encoding="utf-8")
                    if paths["record"].exists() else ""
                )
                self._json(parse_record_md(text))
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
        elif path == "/api/log":
            lines, _ = tail_log_lines()
            self._json({"lines": lines[-60:]})
        elif path == "/apple-touch-icon.png":
            self._send(200, icon_bytes("apple-touch-icon.png"), "image/png")
        elif path == "/favicon.png":
            self._send(200, icon_bytes("favicon.png"), "image/png")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            self._send(403, b'{"error":"forbidden host"}')
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(min(length, 65536))
        path = self.path.split("?")[0]
        if path != "/api/refresh":
            self._json({"error": "not found"}, 404)
            return
        if self.headers.get("X-Panel-Token", "") != TOKEN:
            self._send(403, b'{"error":"invalid token"}')
            return
        ok, message = start_refresh()
        self._json({"ok": ok, "message": message})

    def log_message(self, *args):
        pass  # 关闭默认访问日志


def main():
    global ALLOWED_HOSTS
    config = load_config()
    port = int(config.get("webui_port", 8787))
    ALLOWED_HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}"}
    ensure_icons(config)
    server = ThreadingHTTPServer(("127.0.0.1", port), PanelHandler)
    server.daemon_threads = True
    print(f"番剧面板运行中: http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
