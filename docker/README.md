# 开发沙箱（Docker）

在隔离容器里跑 Claude Code 与本项目：**只读**挂载微信加密数据库，**读写**挂载项目目录。

## 用法

```bash
./dev.sh            # 构建/复用镜像并进入 Claude Code（默认 YOLO 模式）
./dev.sh bash       # 进入 shell 调试
./dev.sh pytest     # 在容器里跑命令
REBUILD=1 ./dev.sh  # 不带缓存重建镜像
```

首次运行会构建镜像（装 Python 依赖、Chromium、Node、sqlcipher、Ruby/Jekyll 等，需几分钟），之后走缓存秒级启动。

## 挂载与隔离

| 宿主机 | 容器内 | 权限 |
|---|---|---|
| `~/Library/Containers/com.tencent.xinWeChat/.../xwechat_files` | 同路径（在 `$HOME=/root` 下）| **只读** |
| 项目目录 | `/workspace` | 读写 |
| 卷 `wechat-dev-local` | `/root/.local` | Claude Code 二进制 + 自更新结果 |
| 卷 `wechat-dev-claude` | `/root/.claude` | 登录态 / 配置 / 会话历史 |

- 微信库挂载点必须落在 `$HOME` 下，因为 `wechat_daily/config.py` 用 `Path.home()` 定位。代码以 `file:...?immutable=1` 打开，配合 `:ro` 双重保证只读。
- 解密密钥 `chatlog-mac/keys.json` 和 `.env`（API key）随项目目录一并进入容器。
- **不**向容器透传 SSH 私钥：公开版站点的推送在宿主机/外部完成，容器只做提交与构建测试，避免密钥进沙箱。

## 公开版站点（Jekyll / Chirpy）

公开版网站源码在 `data/public_repo`（独立 git 仓库，GitHub Pages）。镜像已预装 Ruby + 全套 Jekyll gem（装在 `/usr/local/bundle`，即 `BUNDLE_PATH`），与该站 CI（`ruby/setup-ruby` + `jekyll b` + `htmlproofer`）同栈，**离线即可构建测试**：

```bash
./dev.sh bash
cd data/public_repo
bundle install                              # gem 已在镜像里，离线秒过，只生成本地 Gemfile.lock
JEKYLL_ENV=production bundle exec jekyll b -d _site   # 构建（_site 已 gitignore）
bundle exec htmlproofer _site --disable-external --no-enforce-https   # 与 CI 同款体检
```

- 镜像里预装 gem 用的是 `docker/jekyll-Gemfile`（`data/public_repo/Gemfile` 的副本，因为 `data/` 被 `.dockerignore` 排除）。站点 Gemfile 改了依赖，记得同步这份副本并 `REBUILD=1 ./dev.sh`。
- 推送：容器里只 `git commit`，**`git push` 在宿主机/外部做**（容器不带 SSH 私钥）。push 到 `main` 后触发 Pages 工作流自动部署。

## Claude Code 版本

用官方原生安装器装进持久卷 `wechat-dev-local`：

- **首次**启动自动安装最新版；
- **每次**启动 `entrypoint.sh` 跑一次 `claude update`，加上其自带的后台自更新，保持最新。

默认以 **YOLO 模式**（`--dangerously-skip-permissions`）进入，跳过所有权限确认 —— 容器已隔离、微信库只读，故安全。以 root 运行时靠 `IS_SANDBOX=1` 解除该标志的 root 限制。

首次进入需在容器里执行一次 `/login`（登录态存在 `wechat-dev-claude` 卷，之后免登录）。
> 注：未把宿主机 `ANTHROPIC_API_KEY` 透传给 Claude Code，以免覆盖订阅登录改走 API 计费；项目代码仍从 `.env` 自行读取该 key。

## 注意

- 容器内 Python 用 `/opt/venv`（已装好依赖），**不要** source 项目里 macOS 的 `.venv`。
- PDF 渲染：容器用 Chromium + Noto CJK 字体；正文字体退回 Noto（宿主机才有苹方 PingFang）。
