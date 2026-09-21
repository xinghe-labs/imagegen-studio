# imagegen studio

[![CI](https://github.com/xinghe-labs/imagegen-studio/actions/workflows/ci.yml/badge.svg)](https://github.com/xinghe-labs/imagegen-studio/actions/workflows/ci.yml)

本地优先的**生图工作台**：生成器为主的中文 Web 界面，跑在 [image-gen](https://github.com/xinghe-labs/image-gen) 引擎上。输入一句提示词（中文口语也行），润色、生成、收藏、复现、变体，一站完成；agent 通道与命令行共用同一份账本，互不干扰。

## 截图

![imagegen studio](docs/screenshot.png)

## 功能

- **文生图** — 提示词 + 模型（编号目录）+ 预设/自定义尺寸 + 1-4 张批量，任务进度实时显示
- **图生图** — 上传 1-4 张参考图（或从图库一键「用作参考」），描述修改方向
- **图库** — sidecar 账本驱动的历史网格：按模型/关键词筛选、只看收藏、★评分写回记录
- **复现** — 每张图自带完整参数（prompt/模型/参数/SHA-256），一键复制复现命令
- **多网关配置** — 多套 `{名称, Base URL, API Key}` 一键切换；key 只存服务端配置文件，页面永不回传
- **认证预留** — 设 `IMAGE_GEN_TOKEN` 即启用访问令牌，为服务器部署零返工

## 快速开始

前置：安装 [image-gen](https://github.com/xinghe-labs/image-gen) skill（`npx skills add xinghe-labs/image-gen`），并配置好网关（`IMAGE_GENERATION_API_KEY` / `IMAGE_GENERATION_BASE_URL`，或在平台里添加 profile）。

```bash
pip install -r requirements.txt
python scripts/imagegen_server.py          # → http://127.0.0.1:8642
```

服务端按以下顺序寻找引擎：`IMAGE_GEN_CLI` 环境变量 → 仓库同级目录 `image-gen/` → 已安装的 skill 目录（`~/.agents/skills` 等）。

### 提示词库

生成面板的「提示词库」按钮打开三层库：**内置精选**（随仓库的 72 条手写英文提示词，8 个中文分类）+ **GitHub 源同步**（在弹层的源管理里添加 raw 地址，支持 markdown 清单和 JSON 数组，`![配图](url)` 行自动挂到相邻提示词上，内容去重）+ **个人收藏**（图库详情「存提示词」）。格式活样例见 [data/sample-source.md](data/sample-source.md)——它本身就是一个可直接添加的源。

### 配置文件

多网关 profile 存于 `~/.codex/imagegen-profiles.json`：

```json
{
  "profiles": [
    {"name": "apiclaw", "base_url": "https://your-provider.example/v1", "api_key": "sk-..."}
  ],
  "active": "apiclaw"
}
```

key 永不出现在任何 API 响应中；更新 profile 时 key 留空即保留原值。

### 部署到服务器

设 `IMAGE_GEN_TOKEN=<随机串>`（或 `--token`）启用认证——客户端需带 `X-Auth-Token` 头或 `?token=` 参数；图库目录用 `IMAGE_GEN_LIBRARY` 指定；建议置于反向代理（HTTPS）之后。完整步骤（systemd / Docker / Caddy-Nginx 反代 / 安全清单）见 [DEPLOY.md](DEPLOY.md)。本地 Windows 双击 `start.bat` 即可启动。

## 开发

```bash
python -m unittest discover -s tests -p "test_*.py"
```

测试完全离线（本地 fake 网关），CI 在 ubuntu 3.10/3.13 + windows 3.11 上跑，并检出 image-gen 仓库作为集成依赖。

## License

[MIT](LICENSE)
