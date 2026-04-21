# services/pageindex/Dockerfile
FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 创建数据目录
RUN mkdir -p /root/.pageindex /data/documents

# 复制配置文件
COPY config.py .

# 设置环境变量
ENV PAGEINDEX_WORK_PATH=/root/.pageindex
ENV PYTHONUNBUFFERED=1

# 启动 PageIndex 服务
CMD ["python", "-m", "pageindex", "serve", "--host", "0.0.0.0", "--port", "8584"]