# 开发沙箱：跑项目 + 在里面跑 Claude Code。
# - 只读挂载微信加密数据库（见 dev.sh），读写挂载项目目录到 /workspace
# - Claude Code 用官方原生安装器装进持久卷，启动时自更新到最新版（见 entrypoint.sh）
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 容器内统一用 /opt/venv，避免误用项目里 macOS 的 .venv
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin \
    # 让 Claude Code 的配置/登录态落到持久卷 /root/.claude
    CLAUDE_CONFIG_DIR=/root/.claude

# ── 系统依赖 ──────────────────────────────────────────────────────────────
#   build-essential / libsqlcipher-dev : 编译 sqlcipher3
#   libpango / libcairo / libgdk-pixbuf / shared-mime-info : WeasyPrint 运行时
#   chromium + fonts-noto-cjk          : pdf.py 的无头 Chrome 主渲染路径
#   zstd                               : message_parser.decompress 解压消息内容（必需）
#   ffmpeg                             : image_decoder 解码 wxgf/HEVC 图片首帧
#   git / openssh-client / curl ...    : Claude Code 日常工具（openssh 供 git over SSH 的 fetch；推送在宿主机做）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config \
        libsqlcipher-dev libsqlcipher0 \
        libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 \
        libffi8 libcairo2 shared-mime-info \
        chromium fonts-noto-cjk fonts-noto-color-emoji \
        zstd ffmpeg \
        git openssh-client curl ca-certificates ripgrep less jq \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js（部分 MCP / npx 工具会用到）──────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Ruby + Jekyll（构建/测试公开版站点 data/public_repo，Chirpy 主题）──────
#   gem 预装进 /usr/local/bundle（BUNDLE_PATH），与 CI 的 jekyll b + htmlproofer 同栈。
#   data/ 被 .dockerignore 排除，故用 docker/jekyll-Gemfile 这份副本预装，离线可构建。
ENV BUNDLE_PATH=/usr/local/bundle \
    BUNDLE_SILENCE_ROOT_WARNING=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ruby-full zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* \
    && gem install bundler
COPY docker/jekyll-Gemfile /opt/jekyll/Gemfile
RUN cd /opt/jekyll && bundle install

# ── Python 依赖（装进 /opt/venv）─────────────────────────────────────────
RUN python -m venv "$VIRTUAL_ENV"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /tmp/requirements.txt

# pdf.py 在 PATH 里找的是 `chromium`，Debian 装的是 /usr/bin/chromium —— 已匹配
ENV CHROME_BIN=/usr/bin/chromium

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["claude"]
