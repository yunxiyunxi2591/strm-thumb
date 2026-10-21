FROM python:3.12-alpine3.20

# ffmpeg 用于截帧; alpine 基础镜像比 debian slim 小得多(整包约 150MB vs 550MB)
RUN apk add --no-cache ffmpeg tzdata

WORKDIR /app
COPY main.py /app/main.py

# 默认配置说明: v1.0.5 起为纯手动模式 —— 没有任何定时/启动自扫, 只有点 Web 上的「立即扫描一次」才开始干活;
#               旧版 CRON/INTERVAL/RUN_ON_START/RUN_ONCE/GEN_STRATEGY 环境变量一律忽略
# 扫描目录不再给默认值: 留空由 Web 上浏览勾选, 或用 SCAN_PATHS 指定(容器内路径, 与 volumes 对应)
ENV TIMELINE=00:03:01 \
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
