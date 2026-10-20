# STRM 缩略图生成器

把 [MoviePilot](https://github.com/jxxghp/MoviePilot) 插件
[**FFmpegStrm缩略图**](https://github.com/imaliang/MoviePilot-Plugins/tree/main/plugins/ffmpegstrmthumb)
（作者 imaliang）改造成的**独立 Docker 容器**：不依赖 MoviePilot，常驻运行，定时扫描目录里的
`.strm` 文件，用 FFmpeg 从 strm 指向的视频里截取一帧，生成 `<片名>-thumb.jpg`
（Emby / Jellyfin / 飞牛影视 等在 TheMovieDb 没有背景图时就用它兜底）。

> 纯 Python 标准库 + ffmpeg，零第三方依赖，单文件 `main.py`，全环境变量配置。

## 用法

```bash
# 1) 把媒体库目录挂到容器内 /link，改 docker-compose.yml 里的 /path/to/media
docker run -d --name strm-thumb --restart unless-stopped \
  -v /path/to/media:/link \
  -e SCAN_PATHS=/link \
  -e TIMELINE=00:03:01 \
  -e GEN_STRATEGY=100=60 \
  chensen2591/strm-thumb:latest

# 或用 compose（推荐）：把 /path/to/media 改成真实路径后
docker compose up -d
docker compose logs -f          # 看扫描/生成日志
```

首次批量补图建议：`RUN_ONCE=1` + `OVERWRITE=0`（只补缺失的），跑完自动退出；
之后把 `RUN_ONCE` 改回 `0` 常驻，配合 `CRON` 定时补新增的。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SCAN_PATHS` | `/link` | 扫描目录，多个用换行/逗号分隔（**容器内**路径，与 volumes 对应） |
| `EXCLUDE_PATHS` | 空 | 排除目录，同样换行/逗号分隔 |
| `TIMELINE` | `00:03:01` | 截取时间点 `HH:MM:SS` |
| `SUFFIX` | `-thumb.jpg` | 缩略图文件名后缀（`<片名>-thumb.jpg`） |
| `OVERWRITE` | `0` | `1`=对所有文件重新生成；`0`=只补缺失的 |
| `GEN_STRATEGY` | `100=60` | 每生成 N 个暂停 M 秒（防网盘/风控限速） |
| `CRON` | 空 | 5 位 cron（**容器内 UTC**），如 `30 20 * * *` = 每天 UTC 20:30 = 北京 04:30 |
| `INTERVAL` | `3600` | 未设 `CRON` 时的循环间隔（秒） |
| `RUN_ONCE` | `0` | `1`=只跑一次就退出（首次补图用） |
| `TIMEOUT` | `300` | 单个文件 ffmpeg 超时（秒），慢速网盘可调大 |
| `FFMPEG_BIN` | `ffmpeg` | ffmpeg 可执行文件路径 |

## 与原版插件的对应关系

| 原插件配置 | 本容器 |
|---|---|
| 定时扫描路径 | `SCAN_PATHS` |
| 定时扫描排除路径 | `EXCLUDE_PATHS` |
| 截取时间 | `TIMELINE` |
| 生成策略 `100=60` | `GEN_STRATEGY` |
| 覆盖生成 | `OVERWRITE` |
| 定时扫描周期（cron） | `CRON` |
| 立即运行一次 | `RUN_ONCE=1` |

行为完全一致：默认只生成缺失的缩略图；缩略图落在 `.strm` **同级目录**，文件名
`<片名>-thumb.jpg`。

## 说明与注意

- **strm 内容**：文件里通常是视频地址（`http(s)://...`）或本地路径，ffmpeg 都能直接读。
- **网络**：strm 指向阿里云盘/115 等直链时，容器需要能访问外网；若链接有时效性，
  扫描周期别太长，建议 `CRON` 每天一次或 `INTERVAL` 设小一点。
- **权限**：缩略图是**写入**挂载目录，确保容器对该目录可写（`chown`/`chmod` 或挂载时给足权限）。
- **时区**：容器内 cron 按 UTC 解释，北京时间 = UTC+8（如想每天北京 04:30 运行，写 `30 20 * * *`）。

## License

MIT（原插件 MIT，感谢 [imaliang](https://github.com/imaliang)）
