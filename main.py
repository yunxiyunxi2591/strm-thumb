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
v1.0.8: 支持自定义 ffmpeg 请求头(FFMPEG_HEADERS 环境变量或 Web 上的
「ffmpeg 请求头」), 网盘直链要求带 User-Agent/Referer 等头时才放行;
ffmpeg 失败时日志带退出码 + 源地址 + 完整错误输出, 方便定位真实原因。
v1.0.9: ffmpeg 失败但看不到原因时(退出码0无产出 / stderr 为空), 自动用
debug 级重跑一次并输出关键诊断行(http 重定向/流信息/为什么放弃)。
v1.0.10: 截取时间点超过视频时长时(ffmpeg 安静失败: 退出码0零输出), 自动
改取首帧兜底保证出图; 目录选择器支持按名称/修改时间排序。
v1.0.11: 每个文件先探片长(ffprobe, 没有就退回 ffmpeg -i), 时间点超过片长
85% 时自动改成片长的 30% 处截取, 日志写明片长——短片(1~3 分钟)不会再出现
「退出码0零输出」; 探片长可用环境变量 PROBE_DURATION=0 关闭(每文件省一次请求)。
"""
import json
import os
import re
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


# 扫描目录默认留空(v1.0.8 起): 在 Web 上浏览勾选, 或用 SCAN_PATHS 环境变量指定
SCAN_PATHS_DEFAULT = _split_multi(_env("SCAN_PATHS", ""))
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
    "ffmpeg_headers": _env("FFMPEG_HEADERS", ""),        # ffmpeg 额外请求头, 每行一个「名称: 值」
}
CFG_SOURCE = "环境变量/默认值"      # 当前配置来源(启动日志用)

FFMPEG = _env("FFMPEG_BIN", "ffmpeg")                           # ffmpeg 可执行文件
FFPROBE = _env("FFPROBE_BIN", "ffprobe")                        # ffprobe(探片长, 没有就退回 ffmpeg)
WEB_PORT = int(_env("WEB_PORT", "8081"))                        # Web 前端端口(改端口需重启容器)
WEB_ENABLED = _env_bool("WEB_ENABLED", "1")                     # 0=关掉前端, 只当 worker

# 可由 Web 编辑的字段(不含 web_port: 改端口要重启 HTTP 服务)
# 注意: v1.0.5 起为纯手动模式, 不再有任何定时/自动扫描相关配置; v1.0.6 起移除排除目录
EDITABLE = ("scan_paths", "timeline", "suffix",
            "overwrite", "file_delay", "batch_size", "batch_rest",
            "timeout", "ffmpeg_headers")

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
        merged["ffmpeg_headers"] = str(merged["ffmpeg_headers"]).strip()
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
    hdrs = str(new.get("ffmpeg_headers", "")).strip()
    if len(hdrs) > 4000:
        return None, "请求头内容过长(上限 4000 字符)"
    out["ffmpeg_headers"] = hdrs
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
                    # name + mtime: 前端可自行按名称/时间排序(v1.0.10)
                    dirs.append({"name": entry.name,
                                 "mtime": int(entry.stat().st_mtime)})
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


def _header_args():
    """配置了 ffmpeg_headers 时返回 -headers 参数(CRLF 分隔, ffmpeg 要求)"""
    hdrs = (CFG.get("ffmpeg_headers") or "").strip()
    if not hdrs:
        return []
    hdrs = "\r\n".join(l.strip() for l in hdrs.splitlines() if l.strip())
    return ["-headers", hdrs]


def _build_ffmpeg_cmd(strm_path, thumb_path, timeline, loglevel="error"):
    """组装 ffmpeg 命令行; timeline=None 表示取首帧(不 seek, 最保底)"""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", loglevel]
    cmd += _header_args()
    if timeline:
        cmd += ["-ss", timeline]
    cmd += ["-i", strm_path,
            "-vframes", "1", "-f", "image2", "-y", thumb_path]
    return cmd


# ---- v1.0.11: 先探片长再决定截取时间点, 根治「时间点超过片长 → 退出码0零输出」 ----
CLAMP_RATIO = 0.30        # 时间点超长时, 改取片长的 30% 处
OVER_RATIO = 0.85         # 时间点超过片长 85% 即算「太长」(太靠片尾多为黑屏/字幕)
PROBE_DURATION = _env("PROBE_DURATION", "1") != "0"   # 0=关闭探片长(每文件省一次请求)

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _tl_seconds(tl):
    """HH:MM:SS -> 秒; 解析失败返回 None"""
    try:
        h, m, s = (int(x) for x in str(tl).split(":"))
        return h * 3600 + m * 60 + s
    except Exception:
        return None


def _fmt_ts(sec):
    sec = max(int(sec), 0)
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


def _probe_duration(url):
    """探片长(秒); 查不到返回 None。
    优先 ffprobe(一次请求就能拿到时长); 镜像里没有 ffprobe 时退回 ffmpeg -i,
    从 stderr 的 Duration 行解析。探不到不影响生成, 只是回到旧的盲 seek 逻辑。"""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1"] + _header_args() + [url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=min(int(CFG["timeout"]), 90))
        for tok in (r.stdout or "").split():
            try:
                v = float(tok)
            except ValueError:
                continue
            if v > 0:
                return v
    except Exception:
        pass
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", url],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=min(int(CFG["timeout"]), 90))
        m = _DUR_RE.search(r.stderr or "")
        if m:
            return (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                    + float(m.group(3)))
    except Exception:
        pass
    return None



# 诊断日志过滤关键词: ffmpeg debug 级输出很长, 只留与"为什么拉流失败"相关的行
_DIAG_KEYS = ("http", "redirect", "Stream", "frame=", "Opening", "Invalid",
              "could not", "error", "Error", "ERROR", "trailing", "moov",
              "seek", "Server returned", "403", "404", "Expired", "expired",
              "unsupported", "Connection", "encoding", "Buid", "Build",
              "Impossible", "impossible", "NULL", "empty", "End of",
              "Protocol", "crypto", "tls", "ssl", "Certificate")


def _filter_debug_lines(stderr):
    """从 ffmpeg debug 输出里筛出关键诊断行(最多 40 行)"""
    lines = [l.strip() for l in (stderr or "").splitlines()
             if l.strip() and any(k in l for k in _DIAG_KEYS)]
    return lines[:40]


def _run_ffmpeg(cmd, thumb_path):
    """跑一次 ffmpeg; 返回 (returncode, stderr, 是否产出有效图片)。
    超时/执行异常时 returncode=None。"""
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=CFG["timeout"])
        ok = (r.returncode == 0 and os.path.exists(thumb_path)
              and os.path.getsize(thumb_path) > 0)
        return r.returncode, (r.stderr or ""), ok
    except subprocess.TimeoutExpired:
        return None, "", False
    except Exception as e:
        return None, repr(e), False


def gen_thumb(strm_path, thumb_path, timeline):
    """ffmpeg 截帧; 成功返回 True。
    strm_path 实为 strm 文件里指向的视频 URL(可能是 http(s) 网盘直链)。

    v1.0.11 截取策略:
    0) 先探片长; 时间点若超过片长 85%, 自动改成片长的 30%(日志里写明片长),
       短片(1~3 分钟)不会再因为 seek 越过片尾而「退出码0零输出且不报错」;
    1) 再按(校正后的)时间点 seek 截取;
    2) 若 seek「安静失败」(退出码0但没产出, stderr 为空——时间点超片长或直链
       不支持中途定位), 自动改取首帧重试;
    3) 本轮连续 3 次 seek 安静失败后, 后续文件直接取首帧, 不再浪费请求;
    4) 取首帧也失败且看不到原因时, debug 级诊断一次(每轮仅一次, 防日志刷屏)。"""
    base = os.path.basename(strm_path)
    rc, err = None, ""
    timeline = timeline or ""

    # ---- v1.0.11: 按片长校正时间点 ----
    if timeline:
        tl = _tl_seconds(timeline)
        dur = _probe_duration(strm_path) if PROBE_DURATION else None
        if dur and tl is not None and tl > dur * OVER_RATIO:
            new_tl = _fmt_ts(dur * CLAMP_RATIO)
            log("片长 %s 短于截取时间点 %s, 改用 %s 截取: %s"
                % (_fmt_ts(dur), timeline, new_tl, base))
            timeline = new_tl
            STATE["seek_miss"] = 0     # 已按片长校正, 不必因历史失败跳过 seek
    how = "seek %s" % (timeline or "(未设)")

    # 第 1 次: 按时间点 seek(本轮已连续失败则跳过, 不再浪费请求)
    if timeline and STATE.get("seek_miss", 0) < 3:
        rc, err, ok = _run_ffmpeg(
            _build_ffmpeg_cmd(strm_path, thumb_path, timeline), thumb_path)
        if ok:
            STATE["seek_miss"] = 0
            return True
        if rc == 0 and not (err or "").strip():
            # 安静失败: 时间点超过片长 / 直链不支持定位。改取首帧兜底。
            STATE["seek_miss"] = STATE.get("seek_miss", 0) + 1
            rc2, err2, ok2 = _run_ffmpeg(
                _build_ffmpeg_cmd(strm_path, thumb_path, None), thumb_path)
            if ok2:
                log("时间点 %s 截取无结果(退出码0零输出: 多半是该点超过了本片时长, "
                    "或直链不支持中途定位), 已自动改取首帧: %s" % (timeline, base))
                return True
            rc, err = rc2, err2
            how = "seek %s + 取首帧" % timeline
    else:
        # 时间点为空, 或本轮 seek 已连续失败: 直接取首帧
        rc, err, ok = _run_ffmpeg(
            _build_ffmpeg_cmd(strm_path, thumb_path, None), thumb_path)
        if ok:
            return True
        how = "取首帧"

    # ---- 两次都失败: 记一条能定位问题的日志 ----
    err = (err or "").strip().replace("\r", "")
    tail = " | ".join([l for l in err.split("\n") if l.strip()][-3:])[:500]
    if rc is None:
        log("ffmpeg 执行异常(%s): %s | 源地址: %s | %s"
            % (how, base, strm_path, (err or "")[:150] or "(无输出)"))
    elif rc == 0:
        log("ffmpeg 退出码0但未产出文件(%s): %s | 源地址: %s | 输出: %s"
            % (how, base, strm_path, tail or "(空)"))
    elif rc < 0:
        log("ffmpeg 被信号 %d 杀死(可能内存不足/OOM, %s): %s | 源地址: %s | 输出: %s"
            % (-rc, how, base, strm_path, tail or "(空)"))
    else:
        log("ffmpeg 失败(退出码 %d, %s): %s | 源地址: %s | 输出: %s"
            % (rc, how, base, strm_path, tail or "(空)"))
    # 看不到原因(退出码0无产出 / stderr 为空)时 debug 诊断一次(每轮仅一次)
    if (rc == 0 or not err) and not STATE.get("diag_done"):
        STATE["diag_done"] = True
        _diag_rerun(strm_path, thumb_path, None)
    return False


def _diag_rerun(strm_path, thumb_path, timeline):
    """失败且看不到原因时, 用 debug 级重跑一次并把关键行写进日志。
    v1.0.10: 每轮扫描只对第一个「查不出原因」的失败文件触发, 避免日志被刷屏。"""
    log("诊断: 失败且无有效错误输出, 用 debug 级重跑一次定位(本轮仅此一次)")
    try:
        dbg = _build_ffmpeg_cmd(strm_path, thumb_path, timeline, "debug")
        r2 = subprocess.run(dbg, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=CFG["timeout"])
        diag = _filter_debug_lines(r2.stderr)
        if diag:
            log("诊断日志(退出码 %s, %d 行关键输出):" % (r2.returncode, len(diag)))
            for l in diag:
                log("  └ " + l[:220])
        else:
            log("诊断日志: debug 级也无任何关键输出(退出码 %s, stderr %d 字节)"
                % (r2.returncode, len(r2.stderr or "")))
    except Exception as e:
        log("诊断重跑异常: %r" % e)


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
                 processed=0, last_start=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 seek_miss=0, diag_done=False)   # v1.0.10: 每轮重置 seek 连击与诊断标记
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

VERSION = "1.0.11"      # 显示在页面标题旁, 便于确认容器跑的是哪版代码

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
.dirsort{display:flex;align-items:center;gap:6px;padding:6px 10px 2px;font-size:12px;
  color:var(--sub);flex-wrap:wrap}
.dirsort .sortbtn{padding:2px 9px;font-size:12px;border-radius:6px;border:none;
  color:#fff;cursor:pointer;opacity:.55}
.dirsort .sortbtn:hover{opacity:.8}
.dirsort .sortbtn.on{opacity:1;box-shadow:0 0 0 2px var(--card),0 0 0 4px var(--green)}
.ditem .dtime{margin-left:auto;flex:none;font-size:11px;color:var(--faint);padding-left:8px}
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
.sellist{margin-top:8px;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center;
  font-size:12px;color:var(--sub)}
.sellist .selitem{display:inline-flex;align-items:center;gap:4px;background:rgba(34,197,94,.12);
  border:1px solid rgba(34,197,94,.35);color:#15803d;border-radius:999px;
  padding:2px 6px 2px 10px;font-weight:700;max-width:100%}
html[data-theme="dark"] .sellist .selitem{color:#4ade80}
.sellist .selitem span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:280px}
.sellist .selitem b{cursor:pointer;opacity:.6;padding:0 2px}
.sellist .selitem b:hover{opacity:1}
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
        <div class="sellist" id="f-sellist"></div>
        <div class="dirpanel" id="f-dirpanel" hidden>
          <div class="crumbs" id="f-crumbs"></div>
          <div class="dirsort">
            <span>排序</span>
            <button type="button" class="gray sortbtn" data-sort="name">按名称</button>
            <button type="button" class="gray sortbtn" data-sort="mtime_desc">时间新→旧</button>
            <button type="button" class="gray sortbtn" data-sort="mtime_asc">时间旧→新</button>
          </div>
          <div class="dirlist" id="f-dirlist"></div>
        </div>
        <span class="hint">点目录名自动进入下一层; 勾上前面的方块 = 确认扫描该目录(含其中的全部 .strm); 首次打开自动停在上次保存的扫描目录; 下面列表里点 ✕ 可移除已选目录</span>
      </div>
      <div class="fld">
        <label>截取时间点</label>
        <input id="f-timeline" placeholder="00:03:01" spellcheck="false">
        <span class="hint">HH:MM:SS; 会先探片长: 时间点超过片长的 85% 时自动改成片长的 30% 处(日志会写片长), 所以填大一点也安全; 想固定取开头就填 00:00:00</span>
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
      <div class="fld wide">
        <label>ffmpeg 请求头(可选)</label>
        <textarea id="f-headers" rows="3" spellcheck="false" placeholder="User-Agent: Mozilla/5.0&#10;Referer: https://115.com/"></textarea>
        <span class="hint">每行一个「名称: 值」(如 User-Agent / Referer / Cookie); 网盘直链要求带头才放行时填写, 留空 = 不带额外请求头</span>
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
let browsePath='/',scanList=[],scanDirty=false,curDirs=[],dirLoaded=false,pendingDefault=null,dirSort='name';
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
function fmtTime(ts){
  if(!ts)return '';
  const d=new Date(ts*1000),p=n=>String(n).padStart(2,'0');
  return String(d.getFullYear()).slice(2)+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());
}
// 目录排序(v1.0.10): 按名称(中文按拼音, 数字按自然数) / 时间新→旧 / 时间旧→新
function sortedDirs(){
  const byName=(a,b)=>a.name.localeCompare(b.name,'zh-Hans-CN',{numeric:true});
  const a=curDirs.slice();
  if(dirSort==='mtime_desc')a.sort((x,y)=>(y.mtime||0)-(x.mtime||0)||byName(x,y));
  else if(dirSort==='mtime_asc')a.sort((x,y)=>(x.mtime||0)-(y.mtime||0)||byName(x,y));
  else a.sort(byName);
  return a;
}
function renderSortBar(){
  const map={name:'f-sort-name',mtime_desc:'f-sort-mtime-desc',mtime_asc:'f-sort-mtime-asc'};
  for(const k in map){
    const b=document.getElementById(map[k]);
    if(b)b.className='gray sortbtn'+(k===dirSort?' on':'');
  }
}
function renderDirList(){
  if(!dirLoaded)return;
  const box=document.getElementById('f-dirlist');
  if(!curDirs.length){ box.innerHTML='<div class="empty">(此目录下没有子目录)</div>'; renderSortBar(); return; }
  const showTime=dirSort!=='name';
  box.innerHTML=sortedDirs().map(x=>{
    const full=browsePath==='/'?'/'+x.name:browsePath+'/'+x.name;
    const on=scanList.indexOf(full)>=0;
    return '<div class="ditem'+(on?' checked':'')+'">'+
      '<input type="checkbox" data-sel="'+esc(full)+'"'+(on?' checked':'')+' title="勾选=扫描此目录">'+
      '<span class="dname" data-dir="'+esc(full)+'" title="点击进入">'+esc(x.name)+'</span>'+
      (showTime?'<span class="dtime" title="修改时间">'+fmtTime(x.mtime)+'</span>':'')+
      '<span class="dgo" data-dir="'+esc(full)+'" title="进入">▸</span></div>';
  }).join('');
  renderSortBar();
}
function toggleScan(p,on){
  const i=scanList.indexOf(p);
  if(on&&i<0){scanList.push(p);scanDirty=true;}
  if(!on&&i>=0){scanList.splice(i,1);scanDirty=true;}
  renderSelList(); renderDirList();
}
function scanRemove(i){
  if(i>=0&&i<scanList.length){scanList.splice(i,1);scanDirty=true;renderSelList();renderDirList();}
}
// 已选目录列表: 常驻在「浏览目录」按钮下方, 收起面板也能看到/移除
function renderSelList(){
  const sc=document.getElementById('f-selcount');
  if(sc)sc.textContent=scanList.length;
  const box=document.getElementById('f-sellist');
  if(!box)return;
  if(!scanList.length){
    box.innerHTML='<span style="color:var(--faint)">尚未选择扫描目录(点上方「浏览目录」展开面板, 勾选要扫描的目录)</span>';
    return;
  }
  box.innerHTML='<span>已选 '+scanList.length+' 个:</span>'+scanList.map((p,i)=>
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
  F('f-timeout',c.timeout);F('f-headers',c.ffmpeg_headers||'');
  // 扫描目录: 用户未在本地增删时, 以后端配置为准
  if(!scanDirty && JSON.stringify(c.scan_paths||[])!==JSON.stringify(scanList)){
    scanList=(c.scan_paths||[]).slice();
    if(!dirLoaded)pendingDefault=scanList[0]||null;   // 首次展开面板时停在这里
    renderSelList(); renderDirList();
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
    timeout:document.getElementById('f-timeout').value,
    ffmpeg_headers:document.getElementById('f-headers').value
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
renderSelList();
// 已选列表(按钮下方常驻)的移除按钮: 单独绑委托, 与面板内的委托分开
const _sl=document.getElementById('f-sellist');
if(_sl)_sl.addEventListener('click',function(e){
  const t=e.target;
  if(t&&t.dataset&&t.dataset.rm!==undefined)scanRemove(+t.dataset.rm);
});
// 目录面板事件委托(面包屑回跳/点名字进入/勾选), 避免在内嵌 JS 里拼 onclick(转义地狱)
const _dp=document.getElementById('f-dirpanel');
_dp.addEventListener('click',function(e){
  const t=e.target;
  if(!t||!t.dataset)return;
  if(t.tagName==='INPUT')return;                       // checkbox 的点击交给 change 处理, 不当成导航
  if(t.dataset.sort!==undefined){dirSort=t.dataset.sort;renderDirList();return;}   // 排序切换
  if(t.dataset.dir!==undefined){browseTo(t.dataset.dir);return;}   // 点目录名进入
  if(t.dataset.p!==undefined){browseTo(t.dataset.p);return;}      // 面包屑回跳
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
    log("  探片长     : %s" % ("开(时间点超过片长 85% 时自动改成片长 30% 处)"
                             if PROBE_DURATION else "关(PROBE_DURATION=0)"))
    log("  覆盖生成   : %s" % ("开" if CFG["overwrite"] else "关(只补缺失)"))
    log("  限速       : 每文件间隔 %ds, 每 %d 个休息 %ds"
        % (CFG["file_delay"], CFG["batch_size"], CFG["batch_rest"]))
    _hdrs = (CFG.get("ffmpeg_headers") or "").strip()
    if _hdrs:
        log("  ffmpeg 请求头: 已设置 %d 行"
            % len([l for l in _hdrs.splitlines() if l.strip()]))
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
