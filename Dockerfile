FROM python:3.12-slim-bookworm

# ffmpeg 用于截帧
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY main.py /app/main.py

# 默认扫描目录(容器内), 用 volumes 挂载真实媒体库
# 注意: 默认不自动扫描(INTERVAL=0/无CRON/RUN_ON_START=0), 启动后点 Web 上的「立即扫描一次」才开始干活
ENV SCAN_PATHS=/link \
    TIMELINE=00:03:01 \
    GEN_STRATEGY=100=60 \
    OVERWRITE=0 \
    INTERVAL=0 \
    RUN_ON_START=0 \
    CONFIG_PATH=/data/config.json \
    WEB_PORT=8081 \
    WEB_ENABLED=1 \
    TZ=Asia/Shanghai

EXPOSE 8081
VOLUME ["/link"]
RUN mkdir -p /data

CMD ["python", "-u", "main.py"]
