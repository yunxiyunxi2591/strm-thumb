FROM python:3.12-slim-bookworm

# ffmpeg 用于截帧
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY main.py /app/main.py

# 默认扫描目录(容器内), 用 volumes 挂载真实媒体库
ENV SCAN_PATHS=/link \
    TIMELINE=00:03:01 \
    GEN_STRATEGY=100=60 \
    OVERWRITE=0 \
    INTERVAL=3600 \
    WEB_PORT=8081 \
    WEB_ENABLED=1 \
    TZ=Asia/Shanghai

EXPOSE 8081
VOLUME ["/link"]

CMD ["python", "-u", "main.py"]
