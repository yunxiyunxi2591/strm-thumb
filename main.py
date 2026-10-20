#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STRM 缩略图生成器 — MoviePilot「FFmpegStrm缩略图」插件的容器化独立版

原插件: https://github.com/imaliang/MoviePilot-Plugins (plugins/ffmpegstrmthumb)
本容器不依赖 MoviePilot, 常驻运行, 定时扫描目录中的 .strm 文件,
用 FFmpeg 从 strm 指向的视频里截取一帧, 保存为 <片名>-thumb.jpg。

自带简易 Web 前端(默认 8081 端口): 查看状态/统计/日志, 手动触发扫描,
并可在页面上编辑全部配置(保存到 /data/config.json, 重启不丢失)。

默认完全手动: 容器启动不自动扫描, 也不循环; 需要定时可在 Web 上配置
cron 或循环间隔, 需要启动即扫可打开「启动时自动扫描」。

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
    "exclude_paths": _split_multi(_env("EXCLUDE_PATHS", "")),  # 排除目录
    "timeline": _env("TIMELINE", "00:03:01"),            # 截取时间点 HH:MM:SS
    "suffix": _env("SUFFIX", "-thumb.jpg"),              # 缩略图后缀
    "overwrite": _env_bool("OVERWRITE", "0"),            # True=覆盖生成(默认只补缺失)
    "gen_strategy": _env("GEN_STRATEGY", "100=60"),      # 每生成 N 个暂停 M 秒
    "cron": _env("CRON", ""),                            # 5 位 cron; 留空=不按 cron 定时
    "interval": int(_env("INTERVAL", "0")),              # 循环间隔(秒); 0=不循环(仅手动)
    "run_on_start": _env_bool("RUN_ON_START", "0"),      # True=启动时自动扫一轮
    "timeout": int(_env("TIMEOUT", "300")),              # 单个文件 ffmpeg 超时(秒)
}
CFG_SOURCE = "环境变量/默认值"      # 当前配置来源(启动日志用)

FFMPEG = _env("FFMPEG_BIN", "ffmpeg")                           # ffmpeg 可执行文件
RUN_ONCE = _env_bool("RUN_ONCE", "0")                           # 1=只跑一次就退出(首次补图用)
WEB_PORT = int(_env("WEB_PORT", "8081"))                        # Web 前端端口(改端口需重启容器)
WEB_ENABLED = _env_bool("WEB_ENABLED", "1")                     # 0=关掉前端, 只当 worker

# 可由 Web 编辑的字段(不含 web_port: 改端口要重启 HTTP 服务)
EDITABLE = ("scan_paths", "exclude_paths", "timeline", "suffix",
            "overwrite", "gen_strategy", "cron", "interval",
            "run_on_start", "timeout")

_STOP = False                # 退出进程
_STOP_SCAN = False           # 停止当前扫描轮
_cfg_changed = threading.Event()   # 配置已保存, 唤醒调度线程重读

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
    """返回当前生效配置(供前端展示与表单填充)"""
    snap = {k: CFG[k] for k in EDITABLE}
    if CFG["cron"]:
        snap["schedule_display"] = "cron: %s" % CFG["cron"]
    elif CFG["interval"] > 0:
        snap["schedule_display"] = "每 %d 秒" % CFG["interval"]
    else:
        snap["schedule_display"] = "仅手动触发"
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
        merged["exclude_paths"] = _as_list(merged["exclude_paths"])
        merged["overwrite"] = _as_bool(merged["overwrite"])
        merged["run_on_start"] = _as_bool(merged["run_on_start"])
        merged["interval"] = max(_as_int(merged["interval"]), 0)
        merged["timeout"] = max(_as_int(merged["timeout"], 300), 10)
        merged["timeline"] = str(merged["timeline"]).strip()
        merged["suffix"] = str(merged["suffix"]).strip()
        merged["gen_strategy"] = str(merged["gen_strategy"]).strip()
        merged["cron"] = str(merged["cron"]).strip()
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
    out["exclude_paths"] = _as_list(new.get("exclude_paths"))
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
    gs = str(new.get("gen_strategy", "")).strip()
    if "=" not in gs:
        return None, "生成策略格式应为 N=M(每生成 N 个暂停 M 秒)"
    try:
        n, m = (int(x) for x in gs.split("=", 1))
        if n <= 0 or m < 0:
            raise ValueError
    except Exception:
        return None, "生成策略应为正整数=非负整数, 如 100=60"
    out["gen_strategy"] = gs
    cron = str(new.get("cron", "")).strip()
    if cron:
        try:
            Cron(cron)
        except Exception as e:
            return None, "cron 表达式错误: %r(需要 5 段: 分 时 日 月 周)" % e
    out["cron"] = cron
    out["interval"] = max(_as_int(new.get("interval")), 0)
    out["run_on_start"] = _as_bool(new.get("run_on_start"))
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
    _cfg_changed.set()                      # 唤醒调度线程重读
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
    """扫描一轮, 返回 (尝试数, 成功数, 跳过数)"""
    if not CFG["scan_paths"]:
        log("扫描目录为空, 无可扫描目录")
        return 0, 0, 0
    global _STOP_SCAN
    _STOP_SCAN = False
    try:
        max_count, delay = CFG["gen_strategy"].split("=")
        max_count, delay = int(max_count), int(delay)
    except Exception:
        max_count, delay = 100, 60
    tried = ok = skipped = 0
    count = 0
    STATE.update(scanning=True, scan_source=source, cur_path="", cur_file="",
                 processed=0, last_start=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        for base in CFG["scan_paths"]:
            if _STOP_SCAN or _STOP:
                break
            root = Path(base)
            if not root.exists():
                log("扫描路径不存在, 跳过: %s" % base)
                continue
            excl = []
            for e in CFG["exclude_paths"]:
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
    """调度线程: 启动时按 RUN_ON_START 决定是否自动扫; 之后仅在用户配置了
    cron / interval 时才定时扫, 否则待命等手动触发(配置保存会唤醒重读)"""
    if CFG["run_on_start"]:
        log("RUN_ON_START=1, 启动时先扫描一轮")
        t, o, s = scan_once("启动")
        log("本轮结束: 尝试 %d / 成功 %d / 跳过(已存在) %d" % (t, o, s))
        if RUN_ONCE:
            log("RUN_ONCE=1, 退出")
            os._exit(0)
    else:
        log("未开启启动自动扫描, 待命中(点「立即扫描一次」开始干活)")
    while not _STOP:
        # 读当前调度配置
        cron = None
        if CFG["cron"]:
            try:
                cron = Cron(CFG["cron"])
            except Exception as e:
                log("CRON 解析失败(%r), 本次不定时" % e)
        if cron:
            nxt = cron.next_after(datetime.now())
            if nxt is None:
                log("cron 无法解析出下一次触发时间, 停止定时")
                cron = None
            else:
                STATE["next_run"] = nxt.strftime("%Y-%m-%d %H:%M")
                wait = max((nxt - datetime.now()).total_seconds(), 0) + 1
                log("下次扫描: %s (还有 %.0f 秒)" % (STATE["next_run"], wait))
                _sleep_interruptible(wait)
        elif CFG["interval"] > 0:
            STATE["next_run"] = "%.0f 秒后" % CFG["interval"]
            log("下次扫描: %s" % STATE["next_run"])
            _sleep_interruptible(CFG["interval"])
        else:
            STATE["next_run"] = "仅手动触发"
            _cfg_changed.wait(timeout=3600)   # 无自动调度, 等配置变更或退出
            _cfg_changed.clear()
            continue
        if _STOP:
            break
        t, o, s = scan_once("定时")
        log("本轮结束: 尝试 %d / 成功 %d / 跳过(已存在) %d" % (t, o, s))
        if RUN_ONCE:
            log("RUN_ONCE=1, 退出")
            os._exit(0)


def _sleep_interruptible(seconds):
    end = time.time() + seconds
    while not _STOP and time.time() < end:
        if _cfg_changed.wait(timeout=min(5, max(end - time.time(), 0.1))):
            _cfg_changed.clear()
            return                      # 配置已改, 立即重读调度


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
.picker select{flex:1;background:var(--bg2);color:var(--ink);border:1px solid rgba(0,0,0,.12);
  border-radius:8px;padding:8px 10px;font-size:13px;font-family:inherit}
html[data-theme="dark"] .picker select{border-color:rgba(255,255,255,.14)}
.picker button,.addrow button{flex:none;padding:8px 12px;font-size:12px;border-radius:8px}
.crumbs{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px;font-size:12px;color:var(--sub)}
.crumbs span{cursor:pointer;padding:2px 6px;border-radius:6px}
.crumbs span:hover{background:var(--track);color:var(--ink)}
.crumbs span.cur{color:var(--ink);font-weight:700;cursor:default}
.crumbs span.cur:hover{background:transparent}
.crumbs i{color:var(--faint);font-style:normal;align-self:center}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{display:inline-flex;align-items:center;gap:6px;background:rgba(34,197,94,.12);
  border:1px solid rgba(34,197,94,.35);color:#15803d;border-radius:999px;
  padding:3px 6px 3px 11px;font-size:12px;font-weight:700;max-width:100%}
html[data-theme="dark"] .chip{color:#4ade80}
.chip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip b{cursor:pointer;opacity:.6;padding:0 3px}
.chip b:hover{opacity:1}
.chips .empty{font-size:12px;color:var(--faint)}
.addrow{display:flex;gap:8px;margin-top:8px}
.addrow input{flex:1;background:var(--bg2);color:var(--ink);border:1px solid rgba(0,0,0,.12);
  border-radius:8px;padding:8px 10px;font-size:13px;font-family:inherit}
html[data-theme="dark"] .addrow input{border-color:rgba(255,255,255,.14)}
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
    <h2>配置(保存后写入 /data/config.json, 重启不丢失)</h2>
    <div class="frm">
      <div class="fld wide">
        <label>扫描目录(下拉进入选择, 可添加多个)</label>
        <div class="picker">
          <select id="f-browse" title="选择子目录后点「进入」"></select>
          <button type="button" class="gray" onclick="browseEnter()">进入 ↓</button>
          <button type="button" onclick="browseAdd()">＋ 添加当前目录</button>
        </div>
        <div class="crumbs" id="f-crumbs"></div>
        <div class="chips" id="f-chips"></div>
        <div class="addrow">
          <input id="f-manual" placeholder="或手动输入容器内路径, 如 /link/电影" spellcheck="false">
          <button type="button" class="gray" onclick="browseAddManual()">添加</button>
        </div>
        <span class="hint">浏览的是容器内目录(把媒体库挂载到 /link 后, 在这里一层层点进去)</span>
      </div>
      <div class="fld wide">
        <label>排除目录(每行一个, 可留空)</label>
        <textarea id="f-excl" spellcheck="false"></textarea>
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
        <label>生成策略(每 N 个暂停 M 秒)</label>
        <input id="f-strategy" placeholder="100=60" spellcheck="false">
      </div>
      <div class="fld">
        <label>定时 cron(5 段: 分 时 日 月 周)</label>
        <input id="f-cron" placeholder="留空 = 不自动定时" spellcheck="false">
        <span class="hint">例 30 4 * * * = 每天 04:30(容器内 UTC)</span>
      </div>
      <div class="fld">
        <label>循环间隔(秒, 0 = 不循环)</label>
        <input id="f-interval" type="number" min="0" step="60">
      </div>
      <div class="fld">
        <label>启动时自动扫描</label>
        <select id="f-runstart">
          <option value="0">关(只手动点扫描)</option>
          <option value="1">开(容器启动先扫一轮)</option>
        </select>
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
// ---- 扫描目录选择器 ----
let browsePath='/',scanList=[],scanDirty=false,pickerInited=false;
async function browseTo(p){
  browsePath=p; renderCrumbs();
  const sel=document.getElementById('f-browse');
  sel.innerHTML='<option value="">(读取中…)</option>';
  let d;
  try{ d=await fetchJSON('/api/browse?path='+encodeURIComponent(p),null,8000); }
  catch(e){ sel.innerHTML='<option value="">(读取失败, 点「进入」重试)</option>'; return; }
  if(!d.dirs||!d.dirs.length){ sel.innerHTML='<option value="">(此目录下没有子目录)</option>'; return; }
  sel.innerHTML='<option value="">— 选择子目录('+d.dirs.length+') —</option>'+
    d.dirs.map(x=>'<option value="'+esc(x)+'">'+esc(x)+'</option>').join('');
  sel.selectedIndex=0;
}
function browseEnter(){
  const sel=document.getElementById('f-browse');
  const v=sel.value; if(!v)return;
  browseTo(browsePath==='/'?'/'+v:browsePath+'/'+v);
}
function browseAdd(){
  if(!browsePath||browsePath==='/'){flashMsg('不能添加根目录 /, 请先进入子目录',true);return;}
  if(scanList.indexOf(browsePath)<0){scanList.push(browsePath);scanDirty=true;renderChips();}
}
function browseAddManual(){
  const el=document.getElementById('f-manual');
  const v=el.value.trim(); if(!v)return;
  if(scanList.indexOf(v)<0){scanList.push(v);scanDirty=true;renderChips();}
  el.value='';
}
function scanRemove(i){scanList.splice(i,1);scanDirty=true;renderChips();}
function renderChips(){
  const box=document.getElementById('f-chips');
  if(!scanList.length){box.innerHTML='<span class="empty">尚未添加扫描目录</span>';return;}
  box.innerHTML=scanList.map((p,i)=>'<span class="chip"><span title="'+esc(p)+'">'+esc(p)+
    '</span><b onclick="scanRemove('+i+')" title="移除">✕</b></span>').join('');
}
function renderCrumbs(){
  const box=document.getElementById('f-crumbs');
  const parts=browsePath.split('/').filter(x=>x);
  let html='<span onclick="browseTo(\'/\')">/</span>',acc='';
  parts.forEach((seg,i)=>{
    acc+='/'+seg;
    html+='<i>›</i>'+(i===parts.length-1?'<span class="cur">'+esc(seg)+'</span>':
      '<span onclick="browseTo(\''+esc(acc)+'\')">'+esc(seg)+'</span>');
  });
  box.innerHTML=html;
}
async function refresh(){
  let d;
  try{
    d=await fetchJSON('/api/state',null,8000);
  }catch(e){
    const m=document.getElementById('meta');
    m.textContent='⚠ 加载失败(点我重试)';
    m.style.cursor='pointer'; m.style.color='#ef4444'; m.title=String(e);
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
  F('f-excl',(c.exclude_paths||[]).join('\n'));
  F('f-timeline',c.timeline);F('f-suffix',c.suffix);
  F('f-overwrite',c.overwrite?'1':'0');F('f-strategy',c.gen_strategy);
  F('f-cron',c.cron||'');F('f-interval',c.interval);
  F('f-runstart',c.run_on_start?'1':'0');F('f-timeout',c.timeout);
  // 扫描目录: 用户未在本地增删时, 以后端配置为准
  if(!scanDirty && JSON.stringify(c.scan_paths||[])!==JSON.stringify(scanList)){
    scanList=(c.scan_paths||[]).slice(); renderChips();
  }
  if(!pickerInited){pickerInited=true; renderChips(); browseTo(scanList[0]||'/');}
  const box=document.getElementById('logs');
  const atBottom=box.scrollTop+box.clientHeight>=box.scrollHeight-30;
  box.textContent=(d.logs||[]).join('\\n')||'(暂无日志)';
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
    scan_paths:scanList.join('\n'),
    exclude_paths:document.getElementById('f-excl').value,
    timeline:document.getElementById('f-timeline').value,
    suffix:document.getElementById('f-suffix').value,
    overwrite:document.getElementById('f-overwrite').value,
    gen_strategy:document.getElementById('f-strategy').value,
    cron:document.getElementById('f-cron').value,
    interval:document.getElementById('f-interval').value,
    run_on_start:document.getElementById('f-runstart').value,
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
refresh(); setInterval(refresh,3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60                     # 单连接读超时, 防止半死连接占住线程

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
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
    log("STRM 缩略图生成器启动")
    log("  配置来源   : %s" % CFG_SOURCE)
    if not ok:
        log("  ⚠ 配置文件读取失败(%s), 已回退环境变量配置" % err)
    log("  扫描目录   : %s" % (CFG["scan_paths"] or "(空)"))
    log("  排除目录   : %s" % (CFG["exclude_paths"] or "(无)"))
    log("  截取时间点 : %s" % CFG["timeline"])
    log("  覆盖生成   : %s" % ("开" if CFG["overwrite"] else "关(只补缺失)"))
    log("  生成策略   : %s" % CFG["gen_strategy"])
    if CFG["cron"]:
        log("  定时       : cron %s (容器内 UTC)" % CFG["cron"])
    elif CFG["interval"] > 0:
        log("  定时       : 每 %d 秒" % CFG["interval"])
    else:
        log("  定时       : 无(仅手动触发)")
    log("  启动扫描   : %s" % ("开(启动即扫一轮)" if CFG["run_on_start"] else "关(手动点扫描才开始)"))
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
