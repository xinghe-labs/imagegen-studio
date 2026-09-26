# 部署指南

把 imagegen studio 从本机搬上一台服务器的完整清单。三件事必须做对：**认证、HTTPS、引擎路径**。

## 0. 前提

- 服务器：Python 3.10+（Docker 路线可跳过）
- 引擎：[image-gen](https://github.com/xinghe-labs/image-gen) 仓库与本项目同级 clone（或用 `IMAGE_GEN_CLI` 指向任意位置）
- 网关：服务器能访问你的图像 API；key 写在服务器的 profiles 文件里（见下）

## 1. 裸机部署

```bash
git clone https://github.com/xinghe-labs/image-gen.git
git clone https://github.com/xinghe-labs/imagegen-studio.git
cd imagegen-studio

pip install -r requirements.txt

# 服务端 profiles（key 只存在服务器这份文件里）
mkdir -p ~/.codex
cat > ~/.codex/imagegen-profiles.json <<'EOF'
{
  "profiles": [{"name": "main", "base_url": "https://your-provider.example/v1", "api_key": "sk-..."}],
  "active": "main"
}
EOF

# 首次：配置模型目录（读一次 /v1/models，之后离线可用）
python ../image-gen/scripts/image_gen.py configure --base-url "https://your-provider.example/v1" --api-key "sk-..."

# 启动（务必带 token！）
IMAGE_GEN_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))") \
IMAGE_GEN_LIBRARY=/var/lib/imagegen \
  python scripts/imagegen_server.py --host 127.0.0.1 --port 8642
```

绑定 `127.0.0.1` + 反代对外是推荐姿势（token 照设，双保险）。直接 `--host 0.0.0.0` 裸奔只适合内网。

## 2. systemd 常驻

`/etc/systemd/system/imagegen-studio.service`：

```ini
[Unit]
Description=imagegen studio web workbench
After=network-online.target

[Service]
User=youruser
WorkingDirectory=/opt/imagegen-studio
Environment=IMAGE_GEN_TOKEN=REPLACE_WITH_LONG_RANDOM
Environment=IMAGE_GEN_LIBRARY=/var/lib/imagegen
ExecStart=/usr/bin/python3 scripts/imagegen_server.py --host 127.0.0.1 --port 8642
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now imagegen-studio
```

## 3. Docker 部署

```bash
# 引擎放同级目录
git clone https://github.com/xinghe-labs/image-gen.git ../image-gen

# 服务端配置（catalog/profiles 挂载到容器 /root/.codex）
mkdir -p config library
# 编辑 config/imagegen-profiles.json（见 README）

IMAGE_GEN_TOKEN=REPLACE_WITH_LONG_RANDOM docker compose up -d --build
```

## 4. 反向代理（HTTPS）

Caddy（自动签证书）：

```
img.example.com {
    reverse_proxy 127.0.0.1:8642
}
```

Nginx 等价：

```nginx
server {
    server_name img.example.com;
    location / {
        proxy_pass http://127.0.0.1:8642;
        proxy_set_header Host $host;
        client_max_body_size 20m;   # 图生图上传参考图
    }
}
```

客户端访问：打开 `https://img.example.com`，首次会 401——在地址后加 `?token=你的TOKEN`，页面会把 token 存入 localStorage 并自动带上 `X-Auth-Token` 头。

## 5. 多用户（可选：一人一库）

多人共用一个实例时，用 users 文件代替单 token（users 模式优先，`IMAGE_GEN_TOKEN` 被忽略）：

```json
# /etc/imagegen-users.json（chmod 600）
{
  "users": [
    {"name": "alice", "token": "为每人各生成一条随机串", "library": "/var/lib/imagegen/alice"},
    {"name": "bob",   "token": "另一条随机串"}
  ]
}
```

```bash
IMAGE_GEN_USERS=/etc/imagegen-users.json \
IMAGE_GEN_LIBRARY=/var/lib/imagegen \
  python scripts/imagegen_server.py --host 127.0.0.1 --port 8642
```

- 每人用自己的 token 访问 `https://img.example.com/?token=自己的串`；
- 历史/统计/生成/参考图全部按各自 `library` 隔离，跨库访问被拒；
- 未写 `library` 的用户落在 `<IMAGE_GEN_LIBRARY>/<name>`；
- profiles（网关 key）仍为实例级共享，由你统一配置；备份脚本对每个人跑一次（`--source` 指向各自的库）。

## 6. 安全清单

- [ ] `IMAGE_GEN_TOKEN` 已设（32+ 位随机串），或用 users 文件给每人独立 token——没设就等于把你的生图 key 公开
- [ ] HTTPS（反代或云厂商证书）；token 明文走 HTTP 会被中间人拿走
- [ ] profiles 文件权限收紧：`chmod 600 ~/.codex/imagegen-profiles.json`（users 文件同样 `chmod 600`）
- [ ] 服务器出网能到你的网关即可，不要把 8642 端口直接暴露公网
- [ ] key 永远只存在服务端 profiles 文件；任何 API 响应都不含它

## 7. 自动更新（可选）

### 网页侧：已内置，无需配置

- 三个前端文件（index.html / app.js / style.css）响应带 `Cache-Control: no-cache` + ETag——浏览器每次打开页面都会做条件请求，服务端更新后**正常刷新即得新版**，无需强刷清缓存；
- 页面打开期间每 5 分钟轮询一次 `/api/meta` 的版本号（切回标签页时也会立即查一次）：服务端升级后，已打开的页面会弹「服务端已更新到 vX.Y.Z → 刷新」提示，点一下就切到新版。

也就是说：服务器代码换成新版并重启后，什么都不用通知用户——他们最多 5 分钟内（或下次切回页面时）收到提示，点「刷新」即完成更新。

### 方案 A：裸机 / systemd——定时 git 更新

`/opt/imagegen/update.sh`（假设仓库在 /opt/imagegen，服务名 imagegen）：

```bash
#!/usr/bin/env bash
set -e
cd /opt/imagegen
git fetch origin
if git diff --quiet main origin/main; then exit 0; fi   # 没有新提交就退出
git pull --ff-only origin main
pip install -r requirements.txt --quiet
systemctl restart imagegen
```

配 systemd timer 每天检查一次：

```ini
# /etc/systemd/system/imagegen-update.service
[Unit]
Description=imagegen studio auto update
[Service]
Type=oneshot
ExecStart=/opt/imagegen/update.sh
```

```ini
# /etc/systemd/system/imagegen-update.timer
[Unit]
Description=imagegen studio daily update check
[Timer]
OnCalendar=*-*-* 04:20:00
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
chmod +x /opt/imagegen/update.sh
systemctl enable --now imagegen-update.timer
```

### 方案 B：Docker——定时重建

引擎是挂载的，更新即拉代码重建镜像：

```bash
crontab -e
# 每天凌晨拉一次代码并重建（有更新才会实际替换容器）
20 4 * * * cd /opt/imagegen && git pull --ff-only --quiet && docker compose up -d --build
```

走 registry 部署的话也可以用 [watchtower](https://containrrr.dev/watchtower/) 自动拉新镜像。

### 方案 C：GitHub Actions——推 tag 即部署

服务器装好 deploy key 后，在仓库加一个 workflow，tag 推送时 SSH 上去执行 update.sh：

```yaml
# .github/workflows/deploy.yml
on:
  push:
    tags: ["v*"]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.DEPLOY_HOST }}
          username: ${{ secrets.DEPLOY_USER }}
          key: ${{ secrets.DEPLOY_KEY }}
          script: cd /opt/imagegen && bash update.sh
```
