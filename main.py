#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STRM 缩略图生成器 — MoviePilot「FFmpegStrm缩略图」插件的容器化独立版

原插件: https://github.com/imaliang/MoviePilot-Plugins (plugins/ffmpegstrmthumb)
本容器不依赖 MoviePilot, 常驻运行, 扫描目录中的 .strm 文件,
用 FFmpeg 从 strm 指向的视频里截取一帧, 保存为 <片名>-thumb.jpg。

自带简易 Web 前端(默认 8081 端口): 查看状态/统计/日志, 手动触发扫描,
并可在页面上编辑全部配置(保存到 /data/config.json, 重启不丢失)。

v1.0.5 起为纯手动模式: 没有任何定时/启动自扫/循环, 只有手动点
「立即扫描一次」才开始干活(旧版 CRON/INTERVAL/RUN_ON_START/RUN_ONCE
环境变量一律忽略)。为防打爆网盘/源站触发风控, 扫描内置限速:
每处理 1 个文件休息 FILE_DELAY 秒, 每 BATCH_SIZE 个再休息 BATCH_REST 秒。

纯 Python 标准库, 零第三方依赖; 初始配置走环境变量, 之后以配置文件为准。
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
from urllib.parse import parse_qs, urlparse

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


SCAN_PATHS_DEFAULT = _split_multi(_env("SCAN_PATHS", "/link"))
CONFIG_PATH = _env("CONFIG_PATH", "/data/config.json")   # 配置文件路径(可挂卷持久化)

# 运行时可调配置: 环境变量/默认值为初始值, 配置文件(若存在)整体覆盖, Web 保存后写回文件
CFG = {
    "scan_paths": SCAN_PATHS_DEFAULT,                    # 扫描目录, 多个换行分隔
    "timeline": _env("TIMELINE", "00:03:01"),            # 截取时间点 HH:MM:SS
    "suffix": _env("SUFFIX", "-thumb.jpg"),              # 缩略图后缀
    "overwrite": _env_bool("OVERWRITE", "0"),            # True=覆盖生成(默认只补缺失)
    "file_delay": int(_env("FILE_DELAY", "5")),          # 每处理 1 个文件后休息(秒), 防打爆源站
    "batch_size": int(_env("BATCH_SIZE", "30")),         # 每处理 N 个文件后进入批量休息
    "batch_rest": int(_env("BATCH_REST", "60")),         # 批量休息时长(秒), 防风控
    "timeout": int(_env("TIMEOUT", "300")),              # 单个文件 ffmpeg 超时(秒)
}
CFG_SOURCE = "环境变量/默认值"      # 当前配置来源(启动日志用)

FFMPEG = _env("FFMPEG_BIN", "ffmpeg")                           # ffmpeg 可执行文件
WEB_PORT = int(_env("WEB_PORT", "8081"))                        # Web 前端端口(改端口需重启容器)
WEB_ENABLED = _env_bool("WEB_ENABLED", "1")                     # 0=关掉前端, 只当 worker

# 可由 Web 编辑的字段(不含 web_port: 改端口要重启 HTTP 服务)
# 注意: v1.0.5 起为纯手动模式, 不再有任何定时/自动扫描相关配置; v1.0.6 起移除排除目录
EDITABLE = ("scan_paths", "timeline", "suffix",
            "overwrite", "file_delay", "batch_size", "batch_rest",
            "timeout")

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
    "next_run": "仅手动触发",    # 下次扫描时间(v1.0.5 纯手动, 恒定显示)
    "logs": deque(maxlen=300),
}
_scan_lock = threading.Lock()


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    STATE["logs"].append(line)
    print(line, flush=True)


def config_snapshot():
    """返回当前生效配置(供前端展示与表单填充)"""
    snap = {k: CFG[k] for k in EDITABLE}
    snap["schedule_display"] = "仅手动触发"   # v1.0.5 起无任何定时, 只有点「立即扫描」才扫
    return snap


# ---------------- 配置文件读写 ----------------

def _as_list(v):
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return _split_multi(v)


def _as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _as_int(v, default=0):
    try:
        return int(str(v).strip())
    except Exception:
        return default


def load_config_file():
    """启动时调用: 配置文件存在则整体覆盖环境变量配置。返回 (ok, err)"""
    global CFG_SOURCE
    try:
        p = Path(CONFIG_PATH)
        if not p.is_file():
            return True, None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False, "配置文件不是 JSON 对象"
        merged = dict(CFG)
        for k in EDITABLE:
            if k in data and data[k] not in (None, ""):
                merged[k] = data[k]
        merged["scan_paths"] = _as_list(merged["scan_paths"])
        merged["overwrite"] = _as_bool(merged["overwrite"])
        merged["timeout"] = max(_as_int(merged["timeout"], 300), 10)
        merged["timeline"] = str(merged["timeline"]).strip()
        merged["suffix"] = str(merged["suffix"]).strip()
        merged["file_delay"] = min(max(_as_int(merged["file_delay"], 5), 0), 600)
        merged["batch_size"] = min(max(_as_int(merged["batch_size"], 30), 1), 1000)
        merged["batch_rest"] = min(max(_as_int(merged["batch_rest"], 60), 0), 3600)
        CFG.update(merged)
        CFG_SOURCE = CONFIG_PATH
        return True, None
    except Exception as e:
        return False, repr(e)


def validate_config(new):
    """校验 Web 提交的配置; 返回 (clean_dict, err_msg)"""
    out = {}
    scan_paths = _as_list(new.get("scan_paths"))
    if not scan_paths:
        return None, "扫描目录不能为空"
    out["scan_paths"] = scan_paths
    tl = str(new.get("timeline", "")).strip()
    parts = tl.split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None, "截取时间点格式应为 HH:MM:SS"
    h, m, s = (int(p) for p in parts)
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        return None, "截取时间点超出范围(HH 0-23, MM/SS 0-59)"
    out["timeline"] = "%02d:%02d:%02d" % (h, m, s)
    suffix = str(new.get("suffix", "")).strip()
    if not suffix:
        return None, "缩略图后缀不能为空"
    out["suffix"] = suffix
    out["overwrite"] = _as_bool(new.get("overwrite"))
    out["file_delay"] = min(max(_as_int(new.get("file_delay"), 5), 0), 600)
    out["batch_size"] = min(max(_as_int(new.get("batch_size"), 30), 1), 1000)
    out["batch_rest"] = min(max(_as_int(new.get("batch_rest"), 60), 0), 3600)
    out["timeout"] = min(max(_as_int(new.get("timeout"), 300), 10), 3600)
    return out, None


def save_config(new):
    """校验 → 更新 CFG → 写文件 → 唤醒调度线程。返回 (ok, msg)"""
    clean, err = validate_config(new)
    if err:
        return False, err
    CFG.update(clean)
    try:
        p = Path(CONFIG_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps({k: CFG[k] for k in EDITABLE},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)                      # 原子替换, 避免写一半
    except Exception as e:
        return False, "配置已生效但写入文件失败: %r(重启后将丢失本次修改)" % e
    log("配置已保存并写入 %s" % CONFIG_PATH)
    return True, "已保存"


def browse_dir(path):
    """列出目录的子目录(供前端下拉选择); 返回 (clean, parent, dirs, err)"""
    try:
        p = Path(path or "/")
        if not p.is_absolute():
            p = Path("/") / p
        p = Path(os.path.normpath(str(p)))
        if not p.is_dir():
            return None, None, None, "目录不存在或不是目录: %s" % path
        dirs = []
        for entry in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            try:
                if entry.is_dir() and not entry.is_symlink():
                    dirs.append(entry.name)
            except Exception:
                continue
            if len(dirs) >= 500:
                break
        parent = str(p.parent) if p != p.parent else None
        return str(p), parent, dirs, None
    except Exception as e:
        return None, None, None, repr(e)


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
                           text=True, timeout=CFG["timeout"])
        if r.returncode == 0 and os.path.exists(thumb_path) and \
                os.path.getsize(thumb_path) > 0:
            return True
        log("ffmpeg 失败(%s): %s" % (os.path.basename(strm_path),
                                     (r.stderr or "").strip().split("\n")[-1][:200]))
        return False
    except subprocess.TimeoutExpired:
        log("ffmpeg 超时(%ss): %s" % (CFG["timeout"], strm_path))
    except FileNotFoundError:
        log("找不到 %s, 请确认镜像内已安装 ffmpeg" % FFMPEG)
    except Exception as e:
        log("ffmpeg 异常: %r" % e)
    return False


def scan_once(source="手动"):
    """扫描一轮, 返回 (尝试数, 成功数, 跳过数)。
    限速: 每个文件处理后休息 file_delay 秒; 每处理 batch_size 个再休息
    batch_rest 秒(防止把网盘/源站打爆触发风控)。休息均可被「停止」打断。"""
    if not CFG["scan_paths"]:
        log("扫描目录为空, 无可扫描目录")
        return 0, 0, 0
    global _STOP_SCAN
    _STOP_SCAN = False
    file_delay = max(int(CFG["file_delay"]), 0)
    batch_size = max(int(CFG["batch_size"]), 1)
    batch_rest = max(int(CFG["batch_rest"]), 0)
    tried = ok = skipped = 0
    batch = 0                                    # 距上次批量休息已处理的文件数
    STATE.update(scanning=True, scan_source=source, cur_path="", cur_file="",
                 processed=0, last_start=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log("本轮限速: 每文件间隔 %ds, 每 %d 个休息 %ds"
        % (file_delay, batch_size, batch_rest))
    try:
        for base in CFG["scan_paths"]:
            if _STOP_SCAN or _STOP:
                break
            root = Path(base)
            if not root.exists():
                log("扫描路径不存在, 跳过: %s" % base)
                continue
            log("开始扫描: %s" % base)
            STATE["cur_path"] = base
            for fp in sorted(root.rglob("*.strm")):
                if _STOP_SCAN or _STOP:
                    log("收到停止信号, 中断扫描")
                    return tried, ok, skipped
                STATE["cur_file"] = str(fp)
                STATE["processed"] += 1
                thumb = fp.with_name(fp.stem + CFG["suffix"])
                if not CFG["overwrite"] and thumb.exists():
                    skipped += 1
                    STATE["skipped_total"] += 1
                    continue
                src = read_strm(fp)
                if not src:
                    log("strm 内容为空, 跳过: %s" % fp)
                    continue
                tried += 1
                if gen_thumb(src, str(thumb), CFG["timeline"]):
                    ok += 1
                    STATE["generated_total"] += 1
                    log("已生成: %s" % thumb.name)
                else:
                    STATE["failed_total"] += 1
                # ---- 限速: 先按文件间隔休息, 再按批量休息 ----
                if _STOP_SCAN or _STOP:
                    log("收到停止信号, 中断扫描")
                    return tried, ok, skipped
                if file_delay > 0:
                    _sleep_stoppable(file_delay)
                batch += 1
                if batch >= batch_size:
                    log("已处理 %d 个, 休息 %d 秒(防风控)" % (batch, batch_rest))
                    if batch_rest > 0:
                        STATE["cur_file"] = "⏸ 批量休息中(%ds, 防风控)" % batch_rest
                        _sleep_stoppable(batch_rest)
                        STATE["cur_file"] = ""
                    batch = 0
            log("目录完成: %s" % base)
    finally:
        STATE.update(scanning=False, cur_path="", cur_file="",
                     last_end=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     last_stats={"tried": tried, "ok": ok, "skipped": skipped})
    return tried, ok, skipped


def _sleep_stoppable(seconds):
    """限速休息: 每 0.2s 检查一次停止信号, 点「停止当前扫描」可立即打断"""
    end = time.time() + max(float(seconds), 0)
    while not _STOP and not _STOP_SCAN and time.time() < end:
        time.sleep(min(0.2, max(end - time.time(), 0.01)))


# ---------------- Web 前端 ----------------

VERSION = "1.0.7"       # 显示在页面标题旁, 便于确认容器跑的是哪版代码

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
.top h1 .ver{font-size:11px;background:var(--track);color:var(--sub);padding:2px 9px;
  border-radius:999px;font-weight:700;margin-left:8px;vertical-align:2px;letter-spacing:.3px}
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
.frm{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px 18px}
.fld{display:flex;flex-direction:column;gap:5px;font-size:12px}
.fld.wide{grid-column:1/-1}
.fld label{color:var(--sub);font-weight:700}
.fld input,.fld select,.fld textarea{background:var(--bg2);color:var(--ink);border:1px solid rgba(0,0,0,.12);
  border-radius:8px;padding:8px 10px;font-size:13px;font-family:inherit;width:100%;transition:border-color .2s}
html[data-theme="dark"] .fld input,html[data-theme="dark"] .fld select,html[data-theme="dark"] .fld textarea{border-color:rgba(255,255,255,.14)}
.fld input:focus,.fld select:focus,.fld textarea:focus{outline:none;border-color:var(--green)}
.fld textarea{resize:vertical;min-height:58px;line-height:1.5}
.fld .hint{color:var(--faint);font-size:11px}
.savebar{display:flex;align-items:center;gap:12px;margin-top:14px}
.savebar .msg{font-size:12px;font-weight:700;color:#16a34a}
.savebar .msg.err{color:#ef4444}
.picker{display:flex;gap:8px;align-items:center}
.picker button{flex:none;padding:8px 12px;font-size:12px;border-radius:8px}
.dirpanel{margin-top:8px;border:1px solid rgba(0,0,0,.12);border-radius:10px;
  background:var(--bg2);overflow:hidden}
html[data-theme="dark"] .dirpanel{border-color:rgba(255,255,255,.14)}
.dirlist{max-height:260px;overflow:auto;padding:4px}
.ditem{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:7px;font-size:13px}
.ditem:hover{background:var(--track)}
.ditem input[type=checkbox]{flex:none;width:15px;height:15px;accent-color:#22c55e;cursor:pointer}
.ditem .dname{flex:1;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ditem .dname:hover{color:var(--green);font-weight:700}
.ditem .dgo{flex:none;color:var(--faint);font-size:11px;cursor:pointer;padding:0 4px}
.ditem .dgo:hover{color:var(--ink)}
.ditem.checked .dname{font-weight:700;color:#15803d}
html[data-theme="dark"] .ditem.checked .dname{color:#4ade80}
.dirlist .empty{padding:10px;font-size:12px;color:var(--faint);text-align:center}
.selbar{border-top:1px dashed rgba(0,0,0,.12);padding:8px 10px;font-size:12px;color:var(--sub);
  display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}
html[data-theme="dark"] .selbar{border-color:rgba(255,255,255,.14)}
.selbar .selitem{display:inline-flex;align-items:center;gap:4px;background:rgba(34,197,94,.12);
  border:1px solid rgba(34,197,94,.35);color:#15803d;border-radius:999px;
  padding:2px 6px 2px 10px;font-weight:700;max-width:100%}
html[data-theme="dark"] .selbar .selitem{color:#4ade80}
.selbar .selitem span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:280px}
.selbar .selitem b{cursor:pointer;opacity:.6;padding:0 2px}
.selbar .selitem b:hover{opacity:1}
.crumbs{display:flex;flex-wrap:wrap;gap:4px;margin:8px 8px 0;font-size:12px;color:var(--sub)}
.crumbs span{cursor:pointer;padding:2px 6px;border-radius:6px}
.crumbs span:hover{background:var(--track);color:var(--ink)}
.crumbs span.cur{color:var(--ink);font-weight:700;cursor:default}
.crumbs span.cur:hover{background:transparent}
.crumbs i{color:var(--faint);font-style:normal;align-self:center}
</style>
</head>
<body>
<div class="wrap">
  <noscript><div style="background:#ef4444;color:#fff;padding:10px 14px;border-radius:10px;
    margin-bottom:12px;font-size:13px;font-weight:700">⚠ 您的浏览器禁用了 JavaScript, 本页面无法工作。
    请开启 JavaScript 后刷新, 或换用 Chrome / Edge 打开。<a href="/api/ping" style="color:#fff">点此测试接口是否通</a></div></noscript>
  <div class="top">
    <h1>🎞️ STRM 缩略图生成器<span class="ver" title="容器运行的代码版本">v__VERSION__</span></h1>
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
    <h2>配置(保存后写入 /data/config.json, 重启不丢失)</h2>
    <div class="frm">
      <div class="fld wide">
        <label>扫描目录(浏览勾选, 可多选)</label>
        <div class="picker">
          <button type="button" class="gray" id="f-browse-toggle" onclick="toggleDirPanel()">▼ 浏览目录(已选 <b id="f-selcount">0</b> 个)</button>
        </div>
        <div class="dirpanel" id="f-dirpanel" hidden>
          <div class="crumbs" id="f-crumbs"></div>
          <div class="dirlist" id="f-dirlist"></div>
          <div class="selbar" id="f-selbar"></div>
        </div>
        <span class="hint">点目录名自动进入下一层; 勾上前面的方块 = 确认扫描该目录(含其中的全部 .strm); 首次打开自动停在上次保存的扫描目录</span>
      </div>
      <div class="fld">
        <label>截取时间点</label>
        <input id="f-timeline" placeholder="00:03:01" spellcheck="false">
      </div>
      <div class="fld">
        <label>缩略图后缀</label>
        <input id="f-suffix" placeholder="-thumb.jpg" spellcheck="false">
      </div>
      <div class="fld">
        <label>覆盖生成</label>
        <select id="f-overwrite">
          <option value="0">关(只补缺失)</option>
          <option value="1">开(全部重新生成)</option>
        </select>
      </div>
      <div class="fld">
        <label>每个文件间隔(秒)</label>
        <input id="f-filedelay" type="number" min="0" max="600" step="1">
        <span class="hint">每处理完 1 个文件休息这么久, 防打爆网盘/源站</span>
      </div>
      <div class="fld">
        <label>每 N 个文件休息一次</label>
        <input id="f-batchsize" type="number" min="1" max="1000" step="1">
        <span class="hint">默认 30 个, 到数就休息下面设置的时长</span>
      </div>
      <div class="fld">
        <label>批量休息时长(秒)</label>
        <input id="f-batchrest" type="number" min="0" max="3600" step="10">
        <span class="hint">默认 60 秒; 休息期间点「停止当前扫描」可立即中断</span>
      </div>
      <div class="fld">
        <label>单文件超时(秒)</label>
        <input id="f-timeout" type="number" min="10" max="3600" step="10">
      </div>
    </div>
    <div class="savebar">
      <button id="btn-save" onclick="saveConfig()">💾 保存配置</button>
      <span class="msg" id="save-msg"></span>
    </div>
  </div>

  <div class="card">
    <h2>运行日志</h2>
    <div class="logbox" id="logs">加载中…</div>
  </div>
</div>
<script>
// 诊断标记: JS 一旦执行立刻改掉初始的"加载中…"
// → 若右上角始终显示"加载中…"(从未变成"JS已运行…"), 说明 JS 根本没执行:
//   浏览器禁用了 JS / 加载的是缓存旧页 / 浏览器太老不支持 async/await
document.getElementById('meta').textContent='JS已运行, 正在连接后端…';
const NL=String.fromCharCode(10);   // 换行符(避开 Python 字符串转义地狱)
function toggleTheme(){
  const h=document.documentElement;
  const t=h.getAttribute('data-theme')==='dark'?'light':'dark';
  h.setAttribute('data-theme',t);
  try{localStorage.setItem('strm-theme',t)}catch(e){}
}
(function(){let s=null;try{s=localStorage.getItem('strm-theme')}catch(e){}
  document.documentElement.setAttribute('data-theme',s||'light')})();

function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
// 带超时的 fetch: 后端无响应时 8 秒放弃, 绝不把页面卡在"加载中"
async function fetchJSON(url,opts,ms){
  const ac=new AbortController();
  const timer=setTimeout(()=>ac.abort(),ms||8000);
  try{
    const r=await fetch(url,Object.assign({},opts||{},{signal:ac.signal}));
    return await r.json();
  }finally{clearTimeout(timer);}
}
function flashMsg(t,err){
  const m=document.getElementById('save-msg');
  m.className=err?'msg err':'msg'; m.textContent=(err?'✕ ':'')+t;
  setTimeout(()=>{m.className='msg';m.textContent='';},3000);
}
// ---- 扫描目录选择器(浏览+勾选) ----
let browsePath='/',scanList=[],scanDirty=false,curDirs=[],dirLoaded=false,pendingDefault=null;
async function browseTo(p){
  browsePath=p; renderCrumbs();
  const box=document.getElementById('f-dirlist');
  box.innerHTML='<div class="empty">(读取中…)</div>';
  let d;
  try{ d=await fetchJSON('/api/browse?path='+encodeURIComponent(p),null,8000); }
  catch(e){ box.innerHTML='<div class="empty">(读取失败, 重新点一次目录名重试)</div>'; return; }
  curDirs=d.dirs||[]; dirLoaded=true;
  renderDirList();
}
function renderDirList(){
  if(!dirLoaded)return;
  const box=document.getElementById('f-dirlist');
  if(!curDirs.length){ box.innerHTML='<div class="empty">(此目录下没有子目录)</div>'; return; }
  box.innerHTML=curDirs.map(x=>{
    const full=browsePath==='/'?'/'+x:browsePath+'/'+x;
    const on=scanList.indexOf(full)>=0;
    return '<div class="ditem'+(on?' checked':'')+'">'+
      '<input type="checkbox" data-sel="'+esc(full)+'"'+(on?' checked':'')+' title="勾选=扫描此目录">'+
      '<span class="dname" data-dir="'+esc(full)+'" title="点击进入">'+esc(x)+'</span>'+
      '<span class="dgo" data-dir="'+esc(full)+'" title="进入">▸</span></div>';
  }).join('');
}
function toggleScan(p,on){
  const i=scanList.indexOf(p);
  if(on&&i<0){scanList.push(p);scanDirty=true;}
  if(!on&&i>=0){scanList.splice(i,1);scanDirty=true;}
  renderSelBar(); renderDirList();
}
function scanRemove(i){
  if(i>=0&&i<scanList.length){scanList.splice(i,1);scanDirty=true;renderSelBar();renderDirList();}
}
function renderSelBar(){
  const sc=document.getElementById('f-selcount');
  if(sc)sc.textContent=scanList.length;
  const bar=document.getElementById('f-selbar');
  if(!bar)return;
  if(!scanList.length){
    bar.innerHTML='<span style="color:var(--faint)">尚未选择扫描目录(勾选上面的目录即可)</span>';
    return;
  }
  bar.innerHTML='<span>已选 '+scanList.length+' 个:</span>'+scanList.map((p,i)=>
    '<span class="selitem"><span title="'+esc(p)+'">'+esc(p)+'</span>'+
    '<b data-rm="'+i+'" title="移除">✕</b></span>').join('');
}
function toggleDirPanel(){
  const panel=document.getElementById('f-dirpanel');
  const open=panel.hidden;
  panel.hidden=!open;
  const btn=document.getElementById('f-browse-toggle');
  btn.innerHTML=(open?'▲ 收起(已选 ':'▼ 浏览目录(已选 ')+
    '<b id="f-selcount">'+scanList.length+'</b> 个)';
  // 首次展开: 有已保存的扫描目录就直接停在该目录(通常是媒体库根, 如 /link), 否则停根目录
  if(open&&!dirLoaded)browseTo(pendingDefault||browsePath||'/');
}
function renderCrumbs(){
  const box=document.getElementById('f-crumbs');
  const parts=browsePath.split('/').filter(x=>x);
  let html='<span data-p="/">/</span>',acc='';
  parts.forEach((seg,i)=>{
    acc+='/'+seg;
    html+='<i>›</i>'+(i===parts.length-1?'<span class="cur">'+esc(seg)+'</span>':
      '<span data-p="'+esc(acc)+'">'+esc(seg)+'</span>');
  });
  box.innerHTML=html;
}
async function refresh(){
  let d;
  try{
    d=await fetchJSON('/api/state',null,8000);
  }catch(e){
    const m=document.getElementById('meta');
    let why = (e&&e.name==='AbortError') ? '请求超时(8秒)' :
             ((e&&e.name==='TypeError') ? '网络不通' : ('加载失败'));
    m.textContent='⚠ '+why+'(点我重试)';
    m.style.cursor='pointer'; m.style.color='#ef4444'; m.title=String(e&&e.stack||e);
    m.onclick=function(){m.onclick=null;m.style.color='';m.style.cursor='';refresh();};
    return;
  }
  const m0=document.getElementById('meta');
  m0.style.color='';m0.style.cursor='';m0.onclick=null;
  m0.textContent = d.scanning? '扫描中 '+d.processed+' 个' : '空闲 · 下次 '+d.next_run;
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
  // 填充配置表单(跳过用户正在编辑的框, 避免输入被打断)
  const F=(id,v)=>{const el=document.getElementById(id);
    if(el&&document.activeElement!==el)el.value=v;};
  F('f-timeline',c.timeline);F('f-suffix',c.suffix);
  F('f-overwrite',c.overwrite?'1':'0');
  F('f-filedelay',c.file_delay);F('f-batchsize',c.batch_size);F('f-batchrest',c.batch_rest);
  F('f-timeout',c.timeout);
  // 扫描目录: 用户未在本地增删时, 以后端配置为准
  if(!scanDirty && JSON.stringify(c.scan_paths||[])!==JSON.stringify(scanList)){
    scanList=(c.scan_paths||[]).slice();
    if(!dirLoaded)pendingDefault=scanList[0]||null;   // 首次展开面板时停在这里
    renderSelBar(); renderDirList();
  }
  const box=document.getElementById('logs');
  const atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-30;
  box.textContent=(d.logs||[]).join(NL)||'(暂无日志)';
  if(atBottom)box.scrollTop=box.scrollHeight;
  document.getElementById('btn-run').disabled=!!d.scanning;
  document.getElementById('btn-stop').disabled=!d.scanning;
}
async function runNow(){
  try{await fetchJSON('/api/run',{method:'POST'},8000);}catch(e){}
  setTimeout(refresh,300);
}
async function stopScan(){
  try{await fetchJSON('/api/stop',{method:'POST'},8000);}catch(e){}
  setTimeout(refresh,300);
}
async function saveConfig(){
  const msg=document.getElementById('save-msg');
  const btn=document.getElementById('btn-save');
  if(!scanList.length){flashMsg('请先添加至少一个扫描目录',true);return;}
  const body={
    scan_paths:scanList.join(NL),
    timeline:document.getElementById('f-timeline').value,
    suffix:document.getElementById('f-suffix').value,
    overwrite:document.getElementById('f-overwrite').value,
    file_delay:document.getElementById('f-filedelay').value,
    batch_size:document.getElementById('f-batchsize').value,
    batch_rest:document.getElementById('f-batchrest').value,
    timeout:document.getElementById('f-timeout').value
  };
  btn.disabled=true; msg.className='msg'; msg.textContent='保存中…';
  try{
    const d=await fetchJSON('/api/config',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)},10000);
    if(d.ok){scanDirty=false; msg.textContent='✓ 已保存, 下次扫描生效'; setTimeout(()=>msg.textContent='',2500);}
    else{flashMsg(d.error||'保存失败',true);}
  }catch(e){flashMsg('网络错误或超时',true);}
  btn.disabled=false; setTimeout(refresh,200);
}
// 状态轮询与目录选择器互相独立: 即使 /api/state 不通, 目录面板照样能用
refresh(); setInterval(refresh,3000);
renderSelBar();
// 目录面板事件委托(面包屑回跳/点名字进入/勾选/移除), 避免在内嵌 JS 里拼 onclick(转义地狱)
const _dp=document.getElementById('f-dirpanel');
_dp.addEventListener('click',function(e){
  const t=e.target;
  if(!t||!t.dataset)return;
  if(t.tagName==='INPUT')return;                       // checkbox 的点击交给 change 处理, 不当成导航
  if(t.dataset.dir!==undefined){browseTo(t.dataset.dir);return;}   // 点目录名进入
  if(t.dataset.p!==undefined){browseTo(t.dataset.p);return;}      // 面包屑回跳
  if(t.dataset.rm!==undefined){scanRemove(+t.dataset.rm);}        // 已选项移除
});
_dp.addEventListener('change',function(e){
  const t=e.target;
  if(t&&t.type==='checkbox'&&t.dataset&&t.dataset.sel!==undefined)toggleScan(t.dataset.sel,t.checked);
});
</script>
</body>
</html>
""".replace("__VERSION__", VERSION)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60                     # 单连接读超时, 防止半死连接占住线程

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        # 主动关闭连接: 避免某些网络环境(NAS 反代/Docker 网络)吞掉 keep-alive
        # 连接上的后续请求, 导致页面能打开但 API 一直挂起; 3 秒轮询的页面开新连接开销可忽略
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            _t0 = time.monotonic()
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
            body = json.dumps(data)
            _dt = time.monotonic() - _t0
            if _dt > 2:      # 正常应毫秒级; 慢了说明服务端有阻塞, 记日志便于排查
                log("⚠ /api/state 响应耗时 %.1f 秒(异常, 正常应毫秒级)" % _dt)
            self._send(200, body)
        elif self.path == "/api/ping":
            # 无需 JS 的连通性测试: 浏览器直接打开这个地址能看到 JSON 即前后端通
            self._send(200, json.dumps({
                "ok": True, "version": VERSION,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "scanning": STATE["scanning"],
            }))
        elif self.path.split("?")[0] == "/api/browse":
            q = parse_qs(urlparse(self.path).query)
            clean, parent, dirs, err = browse_dir((q.get("path") or ["/"])[0])
            if err:
                self._send(400, json.dumps({"error": err}, ensure_ascii=False))
            else:
                self._send(200, json.dumps(
                    {"path": clean, "parent": parent, "dirs": dirs}))
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
        elif self.path == "/api/config":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                new = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                self._send(400, '{"error":"请求体不是合法 JSON"}')
                return
            if STATE["scanning"]:
                self._send(409, '{"error":"正在扫描中, 请先停止或等本轮结束再保存配置"}')
                return
            ok, msg = save_config(new)
            if ok:
                self._send(200, json.dumps({"ok": True, "message": msg,
                                            "config": config_snapshot()}))
            else:
                self._send(400, json.dumps({"error": msg}, ensure_ascii=False))
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

    ok, err = load_config_file()
    log("=" * 56)
    log("STRM 缩略图生成器启动  v%s" % VERSION)
    log("  配置来源   : %s" % CFG_SOURCE)
    if not ok:
        log("  ⚠ 配置文件读取失败(%s), 已回退环境变量配置" % err)
    log("  扫描目录   : %s" % (CFG["scan_paths"] or "(空)"))
    log("  截取时间点 : %s" % CFG["timeline"])
    log("  覆盖生成   : %s" % ("开" if CFG["overwrite"] else "关(只补缺失)"))
    log("  限速       : 每文件间隔 %ds, 每 %d 个休息 %ds"
        % (CFG["file_delay"], CFG["batch_size"], CFG["batch_rest"]))
    log("  扫描方式   : 仅手动(点 Web 上的「立即扫描一次」才开始, 无任何定时/自动扫描)")
    # 旧版环境变量提示: 定时类 v1.0.5 起忽略, 排除目录 v1.0.6 起忽略
    _legacy = [k for k in ("CRON", "INTERVAL", "RUN_ON_START", "RUN_ONCE",
                           "GEN_STRATEGY", "EXCLUDE_PATHS")
               if os.environ.get(k) not in (None, "")]
    if _legacy:
        log("  ⚠ 检测到旧版环境变量 %s —— 定时类 v1.0.5 起、排除目录 v1.0.6 起已全部忽略"
            % ",".join(_legacy))
    log("  前端       : %s" % ("http://0.0.0.0:%d" % WEB_PORT if WEB_ENABLED else "关闭"))
    log("=" * 56)

    if WEB_ENABLED:
        srv = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("待命中: 只有手动点「立即扫描一次」才会开始扫描")

    while not _STOP:
        time.sleep(1)
    log("已退出")


if __name__ == "__main__":
    sys.exit(main())
