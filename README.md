# STRM 缩略图生成器

把 [MoviePilot](https://github.com/jxxghp/MoviePilot) 插件
[**FFmpegStrm缩略图**](https://github.com/imaliang/MoviePilot-Plugins/tree/main/plugins/ffmpegstrmthumb)
（作者 imaliang）改造成的**独立 Docker 容器**：不依赖 MoviePilot，常驻运行，扫描目录里的
`.strm` 文件，用 FFmpeg 从 strm 指向的视频里截取一帧，生成 `<片名>-thumb.jpg`
（Emby / Jellyfin / 飞牛影视 等在 TheMovieDb 没有背景图时就用它兜底）。

> 纯 Python 标准库 + ffmpeg，零第三方依赖，单文件 `main.py`。
> **自带 Web 前端**（默认 8081 端口）：实时看扫描状态/统计/日志，点按钮立即跑一轮，
> **所有配置都能在页面上改**（保存到 `/data/config.json`，重启不丢失）。
>
> **v1.0.5 起纯手动**：容器没有任何定时/启动自扫/循环，只有点「▶ 立即扫描一次」才开始干活；
> 内置限速（每文件间隔 + 批量休息），防止把网盘/源站打爆触发风控。

## Web 前端

容器启动后访问 `http://NAS的IP:8081`（端口可用 `WEB_PORT` 改）：

- **运行状态**：扫描中/空闲、触发来源（手动）、当前目录、本轮已处理、扫描方式（仅手动触发）
- **累计统计**：已生成 / 跳过（已存在）/ 失败 / 上轮结果
- **操作按钮**：▶ 立即扫描一次、⏹ 停止当前扫描
- **配置表单**：扫描目录（**下拉选择器**：逐层进入容器内目录、面包屑导航、已选目录标签化管理，也支持手动输入路径添加）、排除目录、截取时间点、缩略图后缀、覆盖生成、
  限速（每个文件间隔/每 N 个休息/休息时长）、单文件超时——**全部可编辑**，💾 保存后写入
  `/data/config.json`（挂载 `./data:/data` 持久化），重启/升级不丢失；保存后下次扫描生效
- **运行日志**：最近 300 行，自动滚动，3 秒刷新
- 右上角 🌓 切换浅色/深色主题
- 后端无响应时前端 8 秒超时并提示"⚠ 加载失败(点我重试)"，不会一直转圈

不需要前端就设 `WEB_ENABLED=0`（或 `WEB_PORT=0`）——但注意 v1.0.5 起只能从 Web 触发扫描，
关掉前端后容器将无事可做（仅适合配合后续可能加入的 API 调用场景）。

## 用法

```bash
# 1) 把媒体库目录挂到容器内 /link，改 docker-compose.yml 里的 /path/to/media
docker run -d --name strm-thumb --restart unless-stopped \
  -p 8081:8081 \
  -v /path/to/media:/link \
  -v ./data:/data \
  -e SCAN_PATHS=/link \
  -e TIMELINE=00:03:01 \
  -e FILE_DELAY=5 \
  -e BATCH_SIZE=30 \
  -e BATCH_REST=60 \
  chensen2591/strm-thumb:latest

# 或用 compose（推荐）：把 /path/to/media 改成真实路径后
docker compose up -d
docker compose logs -f          # 看扫描/生成日志
```

启动后打开 `http://NAS的IP:8081`，按需改配置并 💾 保存，点「▶ 立即扫描一次」开始干活。

首次批量补图建议：Web 配置里「覆盖生成」保持**关**（只补缺失），先手动跑一轮；
想全量重建再临时打开。

> **v1.0.5 起为纯手动模式（代码层面）**：没有任何定时 / 启动自扫 / 循环，
> 只有手动点「立即扫描一次」才会扫描。旧版的 `CRON` / `INTERVAL` /
> `RUN_ON_START` / `RUN_ONCE` / `GEN_STRATEGY` 环境变量**一律忽略**
> （启动日志会列出被忽略的变量）。如果你用的是旧 compose 且里面写了
> `INTERVAL: "3600"` 之类的定时配置，升级后这些设置自动失效，无需手动清理。

为防止把网盘/源站打爆触发风控，扫描内置**限速**：每处理 1 个文件休息
`FILE_DELAY` 秒，每 `BATCH_SIZE` 个再休息 `BATCH_REST` 秒（默认 5s / 30 个 / 60s，
均可在 Web 上调整）。限速期间点「⏹ 停止当前扫描」可立即中断。

## 环境变量（初始值）

环境变量只在**没有配置文件**时作为初始值；一旦 Web 上保存过配置，
`/data/config.json` 整体优先（删掉该文件即可回到环境变量模式）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SCAN_PATHS` | `/link` | 扫描目录，多个用换行/逗号分隔（**容器内**路径，与 volumes 对应） |
| `EXCLUDE_PATHS` | 空 | 排除目录，同样换行/逗号分隔 |
| `TIMELINE` | `00:03:01` | 截取时间点 `HH:MM:SS` |
| `SUFFIX` | `-thumb.jpg` | 缩略图文件名后缀（`<片名>-thumb.jpg`） |
| `OVERWRITE` | `0` | `1`=对所有文件重新生成；`0`=只补缺失的 |
| `FILE_DELAY` | `5` | 每处理完 1 个文件休息（秒），防打爆源站 |
| `BATCH_SIZE` | `30` | 每处理多少个文件后进入批量休息 |
| `BATCH_REST` | `60` | 批量休息时长（秒），防风控 |
| `TIMEOUT` | `300` | 单个文件 ffmpeg 超时（秒），慢速网盘可调大 |
| `CONFIG_PATH` | `/data/config.json` | 配置文件路径（Web 保存的配置写这里） |
| `FFMPEG_BIN` | `ffmpeg` | ffmpeg 可执行文件路径 |
| `WEB_PORT` | `8081` | Web 前端端口；设 `0` 关闭前端（**改端口需重启容器**） |
| `WEB_ENABLED` | `1` | `0`=关掉前端（关后将无法触发扫描，纯手动模式无其他入口） |

## 与原版插件的对应关系

| 原插件配置 | 本容器 |
|---|---|
| 定时扫描路径 | `SCAN_PATHS` |
| 定时扫描排除路径 | `EXCLUDE_PATHS` |
| 截取时间 | `TIMELINE` |
| 生成策略 `100=60` | `FILE_DELAY` / `BATCH_SIZE` / `BATCH_REST`（拆成文件间隔+批量休息，限速更细） |
| 覆盖生成 | `OVERWRITE` |
| 定时扫描周期（cron） | 已移除（v1.0.5 纯手动） |
| 立即运行一次 | 已移除（Web 上点「立即扫描一次」） |

行为完全一致：默认只生成缺失的缩略图；缩略图落在 `.strm` **同级目录**，文件名
`<片名>-thumb.jpg`。

## 说明与注意

- **strm 内容**：文件里通常是视频地址（`http(s)://...`）或本地路径，ffmpeg 都能直接读。
- **网络**：strm 指向阿里云盘/115 等直链时，容器需要能访问外网；若担心风控，
  把 `FILE_DELAY` / `BATCH_REST` 调大即可，宁慢勿快。
- **权限**：缩略图是**写入**挂载目录，确保容器对该目录可写（`chown`/`chmod` 或挂载时给足权限）。
- **配置迁移**：旧版 `/data/config.json` 里的 `cron`/`interval`/`run_on_start`/`gen_strategy`
  字段会被自动忽略；在 Web 上保存一次配置即重写为新格式。

## License

MIT（原插件 MIT，感谢 [imaliang](https://github.com/imaliang)）
