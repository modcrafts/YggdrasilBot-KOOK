# Docker Compose 正式部署指南

适合单台 Linux 服务器快速上线 YggdrasilBot-KOOK（自动包含 ParadeDB Postgres、Redis、迁移任务、bot 主进程、worker、scheduler，以及可选的 render / browser 服务）。

## 1. 准备环境变量

```bash
cp .env.compose.example .env
# 生成 YGG_SECRET_KEY（务必备份）
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

编辑 `.env`，至少填写：
- `POSTGRES_PASSWORD`：强密码
- `YGG_SECRET_KEY`：上面生成的 Fernet 密钥
- `YGG_SUPERUSERS`：你的 KOOK 用户 ID（JSON 字符串数组，如 `["12345678"]`）
- `YGG_ADMIN_EMAILS`：控制台管理员邮箱（如 `["admin@example.com"]`）
- `KOOK_BOT_TOKEN`（可选，也可启动后在控制台「设置 → KOOK 连接」里填写）

## 2. 启动服务

### 常规部署（4GB+ 内存，可本地构建）

```bash
docker compose up -d --build
```

### 2GB 内存低配部署（推荐直接拉取 GHCR 预构建镜像，免服务器编译）

每次 push 到 `main` 后，GitHub Actions（`.github/workflows/ci.yaml`）会自动构建并发布 `ghcr.io/modcrafts/yggdrasil-bot:latest`。服务器上直接拉镜像 + 叠加 `docker-compose.lowmem.yml`（Postgres 内存调优、Redis 64MB、`worker --workers 1`、容器内存上限），常驻内存约 750MB–1.1GB：

```bash
docker compose -f docker-compose.yml -f docker-compose.lowmem.yml pull
docker compose -f docker-compose.yml -f docker-compose.lowmem.yml up -d
```

> 2GB 内存环境下**不要**开启 `render` / `browser` profile（均为 Chromium），并建议挂 2GB swap 作为峰值兜底。

如需要开启图片渲染（`render_*` 工具）或浏览器抓网页（`read_url` Chrome 兜底），先在 `.env` 取消 `YGG_RENDER_URL` / `YGG_BROWSER_URL` 注释（也可顺手取消 `COMPOSE_PROFILES=full` 注释，后续就不用每次敲 `--profile`），再直接拉取 GHCR 预构建镜像启动（无需本地构建）：

```bash
# 仅加 render
docker compose --profile render pull && docker compose --profile render up -d
# 同时加 render + browser
docker compose --profile full pull && docker compose --profile full up -d
```

> 注：如果 GHCR 镜像包是 Private，拉取前先执行 `echo <GITHUB_PAT> | docker login ghcr.io -u <GitHub用户名> --password-stdin`。
> 另外，**`sandbox` 工作区服务目前仅支持 Kubernetes**（依赖 `kubernetes-sigs/agent-sandbox` CRD 与 gVisor，参见 [`deploy/README.md`](../README.md#9-sandbox-workspaces-optional)），不在 Docker Compose 中提供。

## 3. 控制台访问与鉴权

- **纯内网 / VPN 访问（关闭控制台鉴权）**：在 `.env` 中设置：
  ```dotenv
  YGG_AUTH_DISABLED=true
  # 绑定内网网卡 IP（例如 192.168.1.10 或 Tailscale IP），确保公网防火墙未放行 8080
  YGG_BIND_HOST=192.168.1.10
  ```
  重启 `bot` 后即可直接访问 `http://<内网IP>:8080`，无需反代和登录头。
- **公网域名访问（保持 `YGG_AUTH_DISABLED=false`）**：保持 `YGG_BIND_HOST=127.0.0.1`，参考 [`Caddyfile.example`](./Caddyfile.example) 用 Caddy + Basic Auth 注入固定 `X-Auth-Request-Email`，或在反代前接 `oauth2-proxy` / Authelia / Cloudflare Access。

## 4. 数据目录、升级与常用运维

所有持久化数据都保存在项目目录内（已在 `.gitignore` 中忽略）：
- `./data/postgres`：PostgreSQL（ParadeDB）数据文件
- `./data/redis`：Redis AOF 持久化文件

```bash
# 查看状态与日志
docker compose ps
docker compose logs -f bot worker

# 拉取新代码并滚动更新（migration 会自动跑完再启动 bot/worker/scheduler）
git pull
docker compose -f docker-compose.yml -f docker-compose.lowmem.yml pull
docker compose -f docker-compose.yml -f docker-compose.lowmem.yml up -d

# 备份 Postgres 数据库
docker compose exec postgres pg_dump -U ygg -Fc ygg > ygg_$(date +%F).dump
```
