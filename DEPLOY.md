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

# 服务端配置（catalog/profiles/characters 挂载到容器 /root/.codex）
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

## 5. 安全清单

- [ ] `IMAGE_GEN_TOKEN` 已设（32+ 位随机串），没设就等于把你的生图 key 公开
- [ ] HTTPS（反代或云厂商证书）；token 明文走 HTTP 会被中间人拿走
- [ ] profiles 文件权限收紧：`chmod 600 ~/.codex/imagegen-profiles.json`
- [ ] 服务器出网能到你的网关即可，不要把 8642 端口直接暴露公网
- [ ] key 永远只存在服务端 profiles 文件；任何 API 响应都不含它
