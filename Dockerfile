FROM python:3.12-slim-bookworm

# ffmpeg 用于截帧
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY main.py /app/main.py

# 默认扫描目录(容器内), 用 volumes 挂载真实媒体库
# 注意: v1.0.5 起为纯手动模式 —— 没有任何定时/启动自扫, 只有点 Web 上的「立即扫描一次」才开始干活;
#       旧版 CRON/INTERVAL/RUN_ON_START/RUN_ONCE/GEN_STRATEGY 环境变量一律忽略
ENV SCAN_PATHS=/link \
    TIMELINE=00:03:01 \
    FILE_DELAY=5 \
    BATCH_SIZE=30 \
    BATCH_REST=60 \
    OVERWRITE=0 \
    CONFIG_PATH=/data/config.json \
    WEB_PORT=8081 \
    WEB_ENABLED=1 \
    TZ=Asia/Shanghai

EXPOSE 8081
VOLUME ["/link"]
RUN mkdir -p /data

CMD ["python", "-u", "main.py"]
