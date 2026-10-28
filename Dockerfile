FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖单独一层，改代码不用重装依赖
COPY requirements.txt .
RUN pip install -r requirements.txt

# 应用代码
COPY app/ ./

# 运行时数据目录（compose 里会用 ./app/data 覆盖挂载）
RUN mkdir -p /app/data

EXPOSE 5100

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5100/health', timeout=3)"

CMD ["python", "bot.py"]
