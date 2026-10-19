FROM python:3.12-slim

LABEL org.opencontainers.image.title="139Strm Pro" \
      org.opencontainers.image.source="https://github.com/sadmion/139strm-pro" \
      org.opencontainers.image.description="Mobile Cloud (139/Yun) STRM generator + 302 direct-link server with native .cas fast-upload playback and 500GB single-file support" \
      org.opencontainers.image.licenses="AGPL-3.0"

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PORT=8025 \
    CONFIG_PATH=/app/config/config.json \
    YUN139_OUTPUT_DIR=/strm

# 依赖单独一层：改代码时不用重装
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt ./
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt \
    || pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/config /strm

COPY yun139/ ./yun139/
COPY templates/ ./templates/
COPY app.py ./

VOLUME ["/app/config", "/strm"]
EXPOSE 8025

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8025')+'/health')" || exit 1

CMD ["python", "app.py"]
