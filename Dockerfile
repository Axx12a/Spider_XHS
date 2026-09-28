# 国内网络拉不到 Docker Hub 时，用镜像源覆盖基础镜像，例如：
#   docker build --build-arg PYTHON_IMAGE=docker.1ms.run/library/python:3.10-slim -t spider_xhs .
ARG PYTHON_IMAGE=python:3.10-slim
FROM ${PYTHON_IMAGE}

# 可选：pip 走国内源，例如 --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX=

WORKDIR /app

# 系统依赖 + Node.js 20：发布/私信的签名算法是 JS，需要 Node 运行时。
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    ca-certificates \
    build-essential \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN python --version && node --version && npm --version

# Python 依赖
COPY requirements.txt ./
RUN if [ -n "$PIP_INDEX" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX" -r requirements.txt; \
    else \
        pip install --no-cache-dir -r requirements.txt; \
    fi

# Node 依赖：xhs_utils/*/js/profile.js 里 require('crypto-js')，
# 原 Dockerfile 缺少这一步，容器内会报 Cannot find module 'crypto-js'。
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY . .

ENV PYTHONUNBUFFERED=1
ENV NODE_ENV=production

# 本仓库没有常驻 Web 服务，原 EXPOSE 5000 无任何进程监听，已移除。
# 默认入口放发布/私信/直播三合一示例，切换 demo.py 顶部的 DEMO_TYPE。
CMD ["python", "demo.py"]
