#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
STRM 缩略图生成器 — MoviePilot「FFmpegStrm缩略图」插件的容器化独立版

原插件: https://github.com/imaliang/MoviePilot-Plugins (plugins/ffmpegstrmthumb)
本容器不依赖 MoviePilot, 常驻运行, 定时扫描目录中的 .strm 文件,
用 FFmpeg 从 strm 指向的视频里截取一帧, 保存为 <片名>-thumb.jpg。

纯 Python 标准库, 零第三方依赖; 全部配置走环境变量。
"""
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
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
FFPROBE_TIMEOUT = int(_env("TIMEOUT", "300"))                   # 单个文件 ffmpeg 超时(秒)
TZ_OFFSET = 0  # 容器内按 UTC 运行, cron 也是 UTC; 需要北京时间就写 "CRON=30 20 * * *"(即北京 4:30)

_STOP = False


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# ---------------- cron 解析(5 段: 分 时 日 月 周) ----------------

def _parse_field(field, lo, hi, is_dow=False):
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
        # 日/周: 标准 cron 语义是两者取"或"
        dom_ok = dt.day in self.dom
        dow_ok = (dt.weekday() + 1) % 7 in self.dow      # python Mon=0 → cron Sun=0
        if self.dom == set(range(1, 32)):
            return True                                  # 日为 * 时只看周
        if self.dow == set(range(0, 7)):
            return dom_ok                               # 周为 * 时只看日
        return dom_ok or dow_ok

    def next_after(self, dt):
        """从 dt 之后(不含当前分钟)找到下一个触发时刻, 找不到则返回 None"""
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
                           text=True, timeout=FFPROBE_TIMEOUT)
        if r.returncode == 0 and os.path.exists(thumb_path) and \
                os.path.getsize(thumb_path) > 0:
            return True
        log("ffmpeg 失败(%s): %s" % (os.path.basename(strm_path),
                                     (r.stderr or "").strip().split("\n")[-1][:200]))
        return False
    except subprocess.TimeoutExpired:
        log("ffmpeg 超时(%ss): %s" % (FFPROBE_TIMEOUT, strm_path))
    except FileNotFoundError:
        log("找不到 %s, 请确认镜像内已安装 ffmpeg" % FFMPEG)
    except Exception as e:
        log("ffmpeg 异常: %r" % e)
    return False


def scan_once(overlay=False):
    """扫描一轮, 返回 (尝试数, 成功数, 跳过数)"""
    if not SCAN_PATHS:
        log("SCAN_PATHS 为空, 无可扫描目录")
        return 0, 0, 0
    try:
        max_count, delay = GEN_STRATEGY.split("=")
        max_count, delay = int(max_count), int(delay)
    except Exception:
        max_count, delay = 100, 60
    tried = ok = skipped = 0
    count = 0
    for base in SCAN_PATHS:
        if _STOP:
            return tried, ok, skipped
        root = Path(base)
        if not root.exists():
            log("扫描路径不存在, 跳过: %s" % base)
            continue
        excl = []
        for e in EXCLUDE_PATHS:
            try:
                excl.append(Path(e).resolve())
            except Exception:
                pass
        log("开始扫描: %s" % base)
        for fp in sorted(root.rglob("*.strm")):
            if _STOP:
                log("收到停止信号, 中断扫描")
                return tried, ok, skipped
            try:
                rp = fp.resolve()
                if any(str(rp).startswith(str(x)) for x in excl):
                    continue
            except Exception:
                pass
            thumb = fp.with_name(fp.stem + SUFFIX)
            if not overlay and thumb.exists():
                skipped += 1
                continue
            src = read_strm(fp)
            if not src:
                log("strm 内容为空, 跳过: %s" % fp)
                continue
            tried += 1
            if gen_thumb(src, str(thumb), TIMELINE):
                ok += 1
                log("已生成: %s" % thumb.name)
            count += 1
            if count >= max_count:
                log("已生成 %d 个, 暂停 %d 秒(防风控)" % (count, delay))
                time.sleep(delay)
                count = 0
        log("目录完成: %s" % base)
    return tried, ok, skipped


def cron_wait(cron):
    """睡到下一个 cron 触发点; 返回 False 表示收到停止信号"""
    while not _STOP:
        nxt = cron.next_after(datetime.now())
        if nxt is None:
            log("cron 无法解析出下一次触发时间, 退回 INTERVAL 循环")
            return None
        wait = (nxt - datetime.now()).total_seconds()
        log("下次扫描: %s (还有 %.0f 秒)" % (nxt.strftime("%Y-%m-%d %H:%M"), max(wait, 0)))
        end = time.time() + max(wait, 0) + 1
        while not _STOP and time.time() < end:
            time.sleep(min(5, max(end - time.time(), 0.1)))
        if not _STOP:
            return True
    return False


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
    log("=" * 56)

    cron = None
    if CRON:
        try:
            cron = Cron(CRON)
        except Exception as e:
            log("CRON 解析失败(%r), 改用每 %d 秒循环" % (e, INTERVAL))

    while not _STOP:
        t, o, s = scan_once(overlay=OVERWRITE)
        log("本轮结束: 尝试 %d / 成功 %d / 跳过(已存在) %d" % (t, o, s))
        if RUN_ONCE:
            log("RUN_ONCE=1, 退出")
            return
        if _STOP:
            break
        if cron:
            cron_wait(cron)
        else:
            end = time.time() + INTERVAL
            while not _STOP and time.time() < end:
                time.sleep(min(5, max(end - time.time(), 0.1)))


if __name__ == "__main__":
    sys.exit(main())
