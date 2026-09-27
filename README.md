# AutoTunnel

把本机应用发布到自己的 Cloudflare 域名。首次登录后，选择本地端口与地址前缀即可创建 Tunnel 和 DNS。后续打开默认进入服务管理，可以添加更多服务、修改名称与地址前缀、切换本地端口、暂停或恢复连接，以及删除 Tunnel 和对应 DNS 记录。

## 启动

需要 Python 3.10+ 和 Node.js 20+。首次登录时会自动下载并校验 `cloudflared`（支持 macOS/Linux 的 x86_64 和 ARM64）；也可自行安装并放入 PATH。域名需已接入 Cloudflare。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
.venv/bin/uvicorn backend.app:app --host 127.0.0.1 --port 18770
```

打开 http://127.0.0.1:18770。开发前端可在 `frontend` 目录执行 `npm run dev`，访问 http://127.0.0.1:5175。

## 数据与运行方式

Cloudflare 授权和 Tunnel 运行凭据保存在 `~/.autotunnel/autotunnel.sqlite3`。目录权限为 `0700`，数据库权限为 `0600`；凭据在本机以明文形式保存，请保护系统账户与备份。可用 `AUTOTUNNEL_DATA_DIR` 更改位置。已有的旧版单 Tunnel 配置会在启动时迁入数据库。

每个服务拥有独立的 Tunnel 和连接进程。开启状态会持久化：AutoTunnel 重启后会尝试恢复已开启的连接；手动暂停的服务保持暂停。删除服务会同步删除 AutoTunnel 管理的 DNS 记录和 Cloudflare Tunnel。Docker 服务需先将 TCP 端口映射到宿主机，才能被连接。

## Linux NAS / Docker 部署

在 NAS 上将本项目放到一个目录，运行：

```bash
docker compose up -d --build
```

如果系统使用独立的 `docker-compose` 命令，改为 `docker-compose up -d --build`。Compose 使用 Linux host 网络，让 AutoTunnel 能连接 NAS 本机的 `127.0.0.1:<端口>`；无需为 AutoTunnel 配置端口映射。数据保存在 `autotunnel-data` 命名卷，容器重建或升级不会清除登录和穿透配置。首次登录时容器会下载并校验 `cloudflared`。从 Mac 迁移时，请先停止 Mac 上的 AutoTunnel，再将 `~/.autotunnel/autotunnel.sqlite3` 复制到 NAS 的数据卷；重新启动后会恢复已启用的穿透。不要让同一个 Tunnel 的两个实例长期同时运行。

默认管理页面仅监听 NAS 的 `127.0.0.1:18770`，可在 NAS 本机打开，或用 SSH 转发访问：

```bash
ssh -L 18770:127.0.0.1:18770 user@nas-address
```

然后在电脑上打开 `http://127.0.0.1:18770`。如果已经有带身份验证的 NAS 反向代理，可设置 `AUTOTUNNEL_BIND_HOST=0.0.0.0` 再运行 Compose，通过反向代理访问管理页面；不要把没有保护的管理 API 直接公开。公开应用自身也应设置登录或访问保护。

Linux host 网络会列出 NAS 本机监听端口，包括已映射到宿主机的容器端口。若还想显示 Docker 容器名称，可选择挂载 Docker socket：

```bash
DOCKER_GID=$(stat -c '%g' /var/run/docker.sock) docker compose -f compose.yaml -f compose.docker-discovery.yaml up -d --build
```

Docker socket 即使以只读卷挂载，也能通过 API 控制 Docker 守护进程；只有信任 AutoTunnel 容器时才启用这一可选功能。容器默认以非 root 用户运行。

对于 Vite 等限制请求主机名的应用，AutoTunnel 会在创建或重启连接时自动检测拦截响应，并为该穿透设置本地 Host。若其他应用需要指定行为，可在新建或编辑服务时选「保留公开域名」或「使用 localhost」。这不是 Mac 特有的问题。

管理 API 仅应监听本机回环地址，或放在带身份验证的反向代理后面。
