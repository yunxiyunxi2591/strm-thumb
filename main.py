#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STRM 缩略图生成器 — MoviePilot「FFmpegStrm缩略图」插件的容器化独立版

原插件: https://github.com/imaliang/MoviePilot-Plugins (plugins/ffmpegstrmthumb)
本容器不依赖 MoviePilot, 常驻运行, 定时扫描目录中的 .strm 文件,
用 FFmpeg 从 strm 指向的视频里截取一帧, 保存为 <片名>-thumb.jpg。

自带简易 Web 前端(默认 8080 端口): 查看状态/统计/日志, 手动触发扫描。

纯 Python 标准库, 零第三方依赖; 全部配置走环境变量。
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------- 配置 ----------------

def _env(k, default):
    v = os.environ.get(k)
    return v if v not in (None, "") else default


def _env_bool(k, default="0"):
    return _env(k, default).strip().lower() in ("1", "true", "yes", "on")


def _split_multi(v):
    """多行/分号/逗号分隔 -> 列表"""
    parts = []
    for chunk in str(v).replace(";", "\n").replace(",", "\n").split("\n"):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


SCAN_PATHS = _split_multi(_env("SCAN_PATHS", "/link"))          # 扫描目录, 多个换行分隔
EXCLUDE_PATHS = _split_multi(_env("EXCLUDE_PATHS", ""))         # 排除目录
TIMELINE = _env("TIMELINE", "00:03:01")                         # 截取时间点 HH:MM:SS
SUFFIX = _env("SUFFIX", "-thumb.jpg")                           # 缩略图后缀
OVERWRITE = _env_bool("OVERWRITE", "0")                         # 1=覆盖生成(默认只补缺失)
GEN_STRATEGY = _env("GEN_STRATEGY", "100=60")                   # 每生成 N 个暂停 M 秒
CRON = _env("CRON", "")                                         # 5 位 cron, 留空则按 INTERVAL 循环
INTERVAL = int(_env("INTERVAL", "3600"))                        # 循环间隔(秒)
RUN_ONCE = _env_bool("RUN_ONCE", "0")                           # 1=只跑一次就退出(首次补图用)
FFMPEG = _env("FFMPEG_BIN", "ffmpeg")                           # ffmpeg 可执行文件
FFMPEG_TIMEOUT = int(_env("TIMEOUT", "300"))                    # 单个文件 ffmpeg 超时(秒)
WEB_PORT = int(_env("WEB_PORT", "8080"))                        # Web 前端端口
WEB_ENABLED = _env_bool("WEB_ENABLED", "1")                     # 0=关掉前端, 只当 worker

_STOP = False                # 退出进程
_STOP_SCAN = False           # 停止当前扫描轮

# ---------------- 状态(供前端展示) ----------------

STATE = {
    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "scanning": False,          # 是否正在扫描
    "scan_source": "",          # 触发来源: 定时/手动
    "cur_path": "",             # 当前扫描目录
    "cur_file": "",             # 当前文件
    "processed": 0,             # 本轮已处理文件数
    "generated_total": 0,       # 累计生成
    "skipped_total": 0,         # 累计跳过(已存在)
    "failed_total": 0,          # 累计失败
    "last_start": None,
    "last_end": None,
    "last_stats": None,         # 上轮 (tried, ok, skipped)
    "next_run": "-",            # 下次扫描时间
    "logs": deque(maxlen=300),
}
_scan_lock = threading.Lock()


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    STATE["logs"].append(line)
    print(line, flush=True)


def config_snapshot():
    return {
        "scan_paths": SCAN_PATHS,
        "exclude_paths": EXCLUDE_PATHS,
        "timeline": TIMELINE,
        "suffix": SUFFIX,
        "overwrite": OVERWRITE,
        "gen_strategy": GEN_STRATEGY,
        "cron": CRON or ("每 %d 秒" % INTERVAL),
        "run_once": RUN_ONCE,
        "timeout": FFMPEG_TIMEOUT,
        "web_port": WEB_PORT if WEB_ENABLED else None,
    }


# ---------------- cron 解析(5 段: 分 时 日 月 周) ----------------

def _parse_field(field, lo, hi):
    """返回该字段允许的取值集合"""
    allowed = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
            if "/" in field and step > 1:      # "5/10" 形式
                end = hi
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                allowed.add(v)
    return allowed


class Cron:
    def __init__(self, expr):
        f = expr.split()
        if len(f) != 5:
            raise ValueError("cron 需要 5 段: 分 时 日 月 周")
        self.minutes = _parse_field(f[0], 0, 59)
        self.hours = _parse_field(f[1], 0, 23)
        self.dom = _parse_field(f[2], 1, 31)
        self.month = _parse_field(f[3], 1, 12)
        dow = _parse_field(f[4], 0, 7)
        if 7 in dow:                     # 周日既可以是 0 也可以是 7
            dow.add(0)
        self.dow = {d % 7 for d in dow}

    def match(self, dt):
        if dt.minute not in self.minutes or dt.hour not in self.hours:
            return False
        if dt.month not in self.month:
            return False
        dom_ok = dt.day in self.dom
        dow_ok = (dt.weekday() + 1) % 7 in self.dow      # python Mon=0 → cron Sun=0
        if self.dom == set(range(1, 32)):
            return True                                  # 日为 * 时只看周
        if self.dow == set(range(0, 7)):
            return dom_ok                                # 周为 * 时只看日
        return dom_ok or dow_ok

    def next_after(self, dt):
        """从 dt 之后(不含当前分钟)找到下一个触发时刻"""
        from datetime import timedelta
        t = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(60 * 24 * 800):
            if self.match(t):
                return t
            t += timedelta(minutes=1)
        return None


# ---------------- 核心逻辑 ----------------

def read_strm(path):
    """读出 strm 指向的视频地址"""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read().strip()
        except UnicodeDecodeError:
            continue
        except Exception:
            return ""
    return ""


def gen_thumb(strm_path, thumb_path, timeline):
    """ffmpeg 截帧; 成功返回 True"""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
           "-ss", timeline, "-i", strm_path,
           "-vframes", "1", "-f", "image2", "-y", thumb_path]
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=FFMPEG_TIMEOUT)
        if r.returncode == 0 and os.path.exists(thumb_path) and \
                os.path.getsize(thumb_path) > 0:
            return True
        log("ffmpeg 失败(%s): %s" % (os.path.basename(strm_path),
                                     (r.stderr or "").strip().split("\n")[-1][:200]))
        return False
    except subprocess.TimeoutExpired:
        log("ffmpeg 超时(%ss): %s" % (FFMPEG_TIMEOUT, strm_path))
    except FileNotFoundError:
        log("找不到 %s, 请确认镜像内已安装 ffmpeg" % FFMPEG)
    except Exception as e:
        log("ffmpeg 异常: %r" % e)
    return False


def scan_once(source="定时"):
    """扫描一轮, 返回 (尝试数, 成功数, 跳过数)"""
    if not SCAN_PATHS:
        log("SCAN_PATHS 为空, 无可扫描目录")
        return 0, 0, 0
    global _STOP_SCAN
    _STOP_SCAN = False
    try:
        max_count, delay = GEN_STRATEGY.split("=")
        max_count, delay = int(max_count), int(delay)
    except Exception:
        max_count, delay = 100, 60
    tried = ok = skipped = 0
    count = 0
    STATE.update(scanning=True, scan_source=source, cur_path="", cur_file="",
                 processed=0, last_start=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        for base in SCAN_PATHS:
            if _STOP_SCAN or _STOP:
                break
            root = Path(base)
            if not root.exists():
                log("扫描路径不存在, 跳过: %s" % base)
                continue
            excl = []
            for e in EXCLUDE_PATHS:
                try:
                    excl.append(str(Path(e).resolve()))
                except Exception:
                    pass
            log("开始扫描: %s" % base)
            STATE["cur_path"] = base
            for fp in sorted(root.rglob("*.strm")):
                if _STOP_SCAN or _STOP:
                    log("收到停止信号, 中断扫描")
                    return tried, ok, skipped
                STATE["cur_file"] = str(fp)
                STATE["processed"] += 1
                try:
                    rp = str(fp.resolve())
                    if any(rp.startswith(x) for x in excl):
                        continue
                except Exception:
                    pass
                thumb = fp.with_name(fp.stem + SUFFIX)
                if not OVERWRITE and thumb.exists():
                    skipped += 1
                    STATE["skipped_total"] += 1
                    continue
                src = read_strm(fp)
                if not src:
                    log("strm 内容为空, 跳过: %s" % fp)
                    continue
                tried += 1
                if gen_thumb(src, str(thumb), TIMELINE):
                    ok += 1
                    STATE["generated_total"] += 1
                    log("已生成: %s" % thumb.name)
                else:
                    STATE["failed_total"] += 1
                count += 1
                if count >= max_count:
                    log("已生成 %d 个, 暂停 %d 秒(防风控)" % (count, delay))
                    time.sleep(delay)
                    count = 0
            log("目录完成: %s" % base)
    finally:
        STATE.update(scanning=False, cur_path="", cur_file="",
                     last_end=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     last_stats={"tried": tried, "ok": ok, "skipped": skipped})
    return tried, ok, skipped


def worker():
    cron = None
    if CRON:
        try:
            cron = Cron(CRON)
        except Exception as e:
            log("CRON 解析失败(%r), 改用每 %d 秒循环" % (e, INTERVAL))
    while not _STOP:
        t, o, s = scan_once()
        log("本轮结束: 尝试 %d / 成功 %d / 跳过(已存在) %d" % (t, o, s))
        if RUN_ONCE:
            log("RUN_ONCE=1, 退出")
            os._exit(0)
        if _STOP:
            break
        if cron:
            nxt = cron.next_after(datetime.now())
            if nxt is None:
                log("cron 无法解析出下一次触发时间, 退回 INTERVAL 循环")
                cron = None
            else:
                STATE["next_run"] = nxt.strftime("%Y-%m-%d %H:%M")
                wait = max((nxt - datetime.now()).total_seconds(), 0) + 1
                log("下次扫描: %s (还有 %.0f 秒)" % (STATE["next_run"], wait))
                _sleep_interruptible(wait)
        else:
            STATE["next_run"] = "%.0f 秒后" % INTERVAL
            _sleep_interruptible(INTERVAL)


def _sleep_interruptible(seconds):
    end = time.time() + seconds
    while not _STOP and time.time() < end:
        time.sleep(min(5, max(end - time.time(), 0.1)))


# ---------------- Web 前端 ----------------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>STRM 缩略图生成器</title>
<style>
:root{--bg1:#eef4f8;--bg2:#f7fafc;--card:#fff;--ink:#1f2937;--sub:#4b5563;--faint:#9ca3af;
  --track:#edf0ed;--green:#22c55e;--amber:#f59e0b;--blue:#3b82f6;--shadow:0 4px 18px rgba(23,58,40,.08)}
html[data-theme="dark"]{--bg1:#0e1712;--bg2:#0b120e;--card:#151f19;--ink:#e6efe8;--sub:#a4b8ab;
  --faint:#71857a;--track:#25312a;--shadow:0 4px 18px rgba(0,0,0,.42)}
*{box-sizing:border-box;margin:0;padding:0}
body{min-height:100vh;background:linear-gradient(168deg,var(--bg1),var(--bg2));
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI","Microsoft YaHei",sans-serif;
  color:var(--ink);padding:16px 12px 40px;transition:background .3s,color .3s}
.wrap{max-width:860px;margin:0 auto}
.top{display:flex;align-items:center;gap:10px;padding:0 4px 14px}
.top h1{font-size:18px;font-weight:800}
.top .st{margin-left:auto;display:flex;align-items:center;gap:7px;font-size:12px;color:var(--sub);
  background:var(--card);border:1px solid rgba(0,0,0,.06);padding:5px 11px;border-radius:999px;
  box-shadow:var(--shadow);font-variant-numeric:tabular-nums}
html[data-theme="dark"] .top .st{border-color:rgba(255,255,255,.08)}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 0 rgba(34,197,94,.45);
  animation:pulse 2.2s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 7px rgba(34,197,94,0)}}
.card{background:var(--card);border-radius:16px;padding:14px 16px;margin-bottom:12px;
  box-shadow:var(--shadow);border:1px solid rgba(255,255,255,.5);transition:background .3s}
html[data-theme="dark"] .card{border-color:rgba(255,255,255,.06)}
.card h2{font-size:13px;color:var(--sub);font-weight:700;margin-bottom:10px;letter-spacing:.5px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px 18px}
.kv{display:flex;justify-content:space-between;align-items:baseline;font-size:13px;gap:8px}
.kv b{font-variant-numeric:tabular-nums}
.kv span{color:var(--sub)}
.bar{height:6px;border-radius:3px;background:var(--track);overflow:hidden;margin-top:8px}
.bar i{display:block;height:100%;border-radius:3px;background:linear-gradient(90deg,#34d399,#a3e635);
  transition:width .8s cubic-bezier(.22,1,.36,1)}
.btns{display:flex;gap:10px;flex-wrap:wrap}
button{border:none;cursor:pointer;font-size:13px;font-weight:700;border-radius:10px;padding:9px 18px;
  color:#fff;background:linear-gradient(135deg,#22c55e,#16a34a);box-shadow:0 3px 10px rgba(34,197,94,.3);
  transition:transform .15s}
button:hover{transform:translateY(-1px)}
button.gray{background:linear-gradient(135deg,#94a3b8,#64748b);box-shadow:0 3px 10px rgba(100,116,139,.3)}
button:disabled{opacity:.5;cursor:not-allowed;transform:none}
.logbox{background:#0d1117;color:#c9d1d9;border-radius:10px;padding:12px 14px;height:300px;overflow-y:auto;
  font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11.5px;line-height:1.7;
  white-space:pre-wrap;word-break:break-all}
.tag{display:inline-block;font-size:11px;padding:2px 9px;border-radius:999px;font-weight:700}
.tag.run{background:rgba(34,197,94,.15);color:#16a34a}
.tag.idle{background:rgba(148,163,184,.18);color:var(--faint)}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1>🎞️ STRM 缩略图生成器</h1>
    <div class="st"><span class="pulse"></span><span id="meta">加载中…</span></div>
    <button class="gray" style="padding:7px 12px" onclick="toggleTheme()" title="切换主题">🌓</button>
  </div>

  <div class="card">
    <h2>运行状态</h2>
    <div class="grid">
      <div class="kv"><span>扫描状态</span><b id="s-state">-</b></div>
      <div class="kv"><span>触发来源</span><b id="s-src">-</b></div>
      <div class="kv"><span>当前目录</span><b id="s-path">-</b></div>
      <div class="kv"><span>本轮已处理</span><b id="s-proc">-</b></div>
      <div class="kv"><span>下次扫描</span><b id="s-next">-</b></div>
      <div class="kv"><span>启动时间</span><b id="s-start">-</b></div>
    </div>
    <div class="bar"><i id="s-bar" style="width:0%"></i></div>
    <div class="btns" style="margin-top:12px">
      <button id="btn-run" onclick="runNow()">▶ 立即扫描一次</button>
      <button id="btn-stop" class="gray" onclick="stopScan()">⏹ 停止当前扫描</button>
    </div>
  </div>

  <div class="card">
    <h2>累计统计</h2>
    <div class="grid">
      <div class="kv"><span>已生成</span><b id="t-gen" style="color:#16a34a">-</b></div>
      <div class="kv"><span>跳过(已存在)</span><b id="t-skip">-</b></div>
      <div class="kv"><span>失败</span><b id="t-fail" style="color:#ef4444">-</b></div>
      <div class="kv"><span>上轮结果</span><b id="t-last">-</b></div>
    </div>
  </div>

  <div class="card">
    <h2>当前配置</h2>
    <div class="grid" id="cfg"></div>
  </div>

  <div class="card">
    <h2>运行日志</h2>
    <div class="logbox" id="logs">加载中…</div>
  </div>
</div>
<script>
function toggleTheme(){
  const h=document.documentElement;
  const t=h.getAttribute('data-theme')==='dark'?'light':'dark';
  h.setAttribute('data-theme',t);
  try{localStorage.setItem('strm-theme',t)}catch(e){}
}
(function(){let s=null;try{s=localStorage.getItem('strm-theme')}catch(e){}
  document.documentElement.setAttribute('data-theme',s||'light')})();

function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function refresh(){
  try{
    const r=await fetch('/api/state'); const d=await r.json();
    document.getElementById('meta').textContent =
      d.scanning? '扫描中 '+d.processed+' 个' : '空闲 · 下次 '+d.next_run;
    const st=document.getElementById('s-state');
    st.innerHTML = d.scanning? '<span class="tag run">扫描中</span>' : '<span class="tag idle">空闲</span>';
    document.getElementById('s-src').textContent=d.scan_source||'-';
    document.getElementById('s-path').textContent=d.cur_path||'-';
    document.getElementById('s-proc').textContent=d.processed;
    document.getElementById('s-next').textContent=d.next_run;
    document.getElementById('s-start').textContent=d.started_at;
    document.getElementById('s-bar').style.width=(d.scanning?'100%':'0%');
    document.getElementById('t-gen').textContent=d.generated_total;
    document.getElementById('t-skip').textContent=d.skipped_total;
    document.getElementById('t-fail').textContent=d.failed_total;
    document.getElementById('t-last').textContent=d.last_stats?
      ('尝试 '+d.last_stats.tried+' / 成功 '+d.last_stats.ok+' / 跳过 '+d.last_stats.skipped):'-';
    const c=d.config;
    document.getElementById('cfg').innerHTML=[
      ['扫描目录',(c.scan_paths||[]).join(' , ')||'-'],
      ['排除目录',(c.exclude_paths||[]).join(' , ')||'-'],
      ['截取时间点',c.timeline],['缩略图后缀',c.suffix],
      ['覆盖生成',c.overwrite?'开':'关(只补缺失)'],
      ['生成策略',c.gen_strategy],['定时',c.cron],
      ['单文件超时',c.timeout+' s'],['前端端口',c.web_port||'已关闭']
    ].map(x=>'<div class="kv"><span>'+esc(x[0])+'</span><b>'+esc(x[1])+'</b></div>').join('');
    const box=document.getElementById('logs');
    const atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-30;
    box.textContent=(d.logs||[]).join('\\n')||'(暂无日志)';
    if(atBottom)box.scrollTop=box.scrollHeight;
    document.getElementById('btn-run').disabled=!!d.scanning;
    document.getElementById('btn-stop').disabled=!d.scanning;
  }catch(e){document.getElementById('meta').textContent='加载失败'}
}
async function runNow(){
  await fetch('/api/run',{method:'POST'}); setTimeout(refresh,300);
}
async function stopScan(){
  await fetch('/api/stop',{method:'POST'}); setTimeout(refresh,300);
}
refresh(); setInterval(refresh,3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            with _scan_lock:
                data = {
                    "scanning": STATE["scanning"],
                    "scan_source": STATE["scan_source"],
                    "cur_path": STATE["cur_path"],
                    "cur_file": STATE["cur_file"],
                    "processed": STATE["processed"],
                    "generated_total": STATE["generated_total"],
                    "skipped_total": STATE["skipped_total"],
                    "failed_total": STATE["failed_total"],
                    "last_stats": STATE["last_stats"],
                    "started_at": STATE["started_at"],
                    "next_run": STATE["next_run"],
                    "config": config_snapshot(),
                    "logs": list(STATE["logs"]),
                }
            self._send(200, json.dumps(data))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path == "/api/run":
            if STATE["scanning"]:
                self._send(409, '{"error":"正在扫描中"}')
                return
            threading.Thread(target=lambda: scan_once("手动"), daemon=True).start()
            self._send(200, '{"ok":true}')
        elif self.path == "/api/stop":
            global _STOP_SCAN
            _STOP_SCAN = True
            self._send(200, '{"ok":true}')
        else:
            self._send(404, '{"error":"not found"}')

    def log_message(self, fmt, *args):
        pass  # 静默 HTTP 日志, 避免刷屏


def main():
    def _sig(*_a):
        global _STOP
        _STOP = True
        log("收到停止信号, 正在退出...")
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    log("=" * 56)
    log("STRM 缩略图生成器启动")
    log("  扫描目录   : %s" % (SCAN_PATHS or "(空)"))
    log("  排除目录   : %s" % (EXCLUDE_PATHS or "(无)"))
    log("  截取时间点 : %s" % TIMELINE)
    log("  覆盖生成   : %s" % ("开" if OVERWRITE else "关(只补缺失)"))
    log("  生成策略   : %s" % GEN_STRATEGY)
    log("  定时       : %s" % (CRON if CRON else "每 %d 秒" % INTERVAL))
    log("  前端       : %s" % ("http://0.0.0.0:%d" % WEB_PORT if WEB_ENABLED else "关闭"))
    log("=" * 56)

    if WEB_ENABLED:
        srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()

    while not _STOP:
        time.sleep(1)
    log("已退出")


if __name__ == "__main__":
    sys.exit(main())
