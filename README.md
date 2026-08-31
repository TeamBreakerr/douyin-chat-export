<div align="center">

# 抖音聊天记录导出工具

**从抖音网页版完整导出私信聊天记录，本地 Web 界面浏览、搜索、导出。**

直接调用抖音 IM 接口（protobuf）抓取，突破网页虚拟列表的滚动上限，可导出完整历史。

</div>

<div align="center">
  <img src="docs/chat-viewer.png" alt="聊天浏览界面" width="820">
</div>

> **目录** ·
> [功能](#功能) ·
> [快速开始](#快速开始) ·
> [部署](#部署) ·
> [使用](#使用) ·
> [控制面板](#控制面板) ·
> [注意事项](#注意事项)

---

## 功能

**采集**
- **完整历史** — 直接调用抖音 IM API（protobuf），突破虚拟列表滚动上限
- **精确排序** — 用服务端 `created_at_us` 单调递增序号排序，消息顺序不乱
- **增量更新** — 增量模式只抓新消息
- **多种消息类型** — 文本、表情包、图片、语音、视频、分享（视频/商品/直播）、一起看视频、引用回复、系统消息
- **语音转文字** — 新消息自动转写，也可一键补充历史语音，支持浏览、搜索和导出

**浏览**（Vue 3 + FastAPI 内置界面）
- 无限滚动、全文搜索、搜索结果一键跳转到原消息
- 消息分组、引用回复区块、图片点击放大、语音/视频在线播放与转写内容展示
- 5 套主题一键切换（暗色 / 微信绿 / 浅色 / 暖棕 / 紫夜），全中文界面

**媒体本地化**
- 图片 AES-GCM 解密 + HEIC 自动转 JPEG；语音自动落地；视频 MPEG-CENC 解密 + faststart 转封装
- 可开关「新消息自动下载」与「回填历史媒体」，避免抖音 CDN 链接过期后失效

**导出 & 运维**
- 导出 [ChatLab](https://github.com/hellodigua/ChatLab) 标准格式（JSON / JSONL），可直接做 AI 聊天分析
- 默认导出文件名包含会话名称和导出时间，多会话文件更易区分
- Web 控制面板：可视化采集 / 导出 / 定时任务 / 远程扫码登录 / 密码保护
- 定时任务（cron）+ [Server酱](https://sct.ftqq.com) 失败推送到微信
- Docker 一键部署，数据持久化到 `./data`

## 快速开始

已装 Docker 即可，无需 clone 仓库、无需本地构建。新建一个目录，保存以下内容为 `docker-compose.yml`：

```yaml
services:
  douyin-chat-export:
    image: ghcr.io/teambreakerr/douyin-chat-export:latest
    container_name: douyin-chat-export
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    environment:
      - TZ=Asia/Shanghai
    restart: unless-stopped
```

```bash
docker compose up -d
```

> **国内拉取 ghcr.io 缓慢或超时？** 把 `image:` 里的 `ghcr.io` 换成 `ghcr.nju.edu.cn`
> （[南京大学镜像代理](https://doc.nju.edu.cn/books/e1654/page/ghcr)），其余不变。

然后打开 `http://localhost:8000/panel` → **登录**（导入 Cookie 或远程扫码，见[登录](#1-登录)），
采集完成后访问 `http://localhost:8000` 浏览。

## 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Docker | >= 20.10 | **推荐**，容器内已含全部依赖 |
| Python | >= 3.10 | 本地运行时的后端与采集器 |
| Node.js | >= 20.19 或 >= 22.12 | 本地运行时构建前端（Vite 7 要求） |

> Docker 用户无需手动装 Python / Node.js，直接看 [Docker 部署](#docker-部署推荐)。

## 部署

### Docker 部署（推荐）

预构建镜像随 main 分支自动发布（amd64 / arm64 双架构），无需 clone、无需本地构建，
compose 写法见[快速开始](#快速开始)。镜像已包含前端构建产物、后端服务、Playwright 浏览器环境，
数据持久化在 `./data`。

| 镜像标签 | 含义 |
|----------|------|
| `latest` | 跟随 main 最新提交 |
| `main-<sha>` | main 上的特定提交，用于 pin 或回滚 |
| `X.Y.Z` / `X.Y` / `X` | 发布版本（打 git tag `vX.Y.Z` 时发布） |

<details>
<summary><b>从源码构建</b>（想改代码或不信任预构建镜像时）</summary>

```bash
git clone https://github.com/TeamBreakerr/douyin-chat-export.git
cd douyin-chat-export
# 取消 docker-compose.yml 里 “build: .” 的注释，然后：
docker compose up -d --build
```

</details>

<details>
<summary><b>环境变量</b></summary>

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MODE` | `all` | `web` 只启动 Web / `scraper` 只采集 / `all` 全部 |
| `HEADLESS` | `true` | 浏览器无头模式（Docker 中必须为 `true`） |
| `SCRAPER_INCREMENTAL` | `true` | 采集是否增量 |
| `SCRAPER_FILTER` | (空) | 过滤指定会话名称 |
| `SCRAPER_SCHEDULE` | (空) | cron 表达式，如 `0 */6 * * *`（空=不定时） |

</details>

<details>
<summary><b>反向代理</b></summary>

默认映射 `8000:8000` 直接访问。如走反向代理（如 Nginx Proxy Manager），去掉 `ports`，
把容器加进反代所在的 Docker 网络：

```yaml
services:
  douyin-chat-export:
    # 删掉 ports 段
    networks:
      - web-internal

networks:
  web-internal:
    external: true
```

</details>

### 本地运行

```bash
git clone https://github.com/TeamBreakerr/douyin-chat-export.git
cd douyin-chat-export

# Python 环境
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 构建前端（Node.js >= 20.19 或 >= 22.12）
cd frontend && npm install && npm run build && cd ..

# 启动
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

浏览 `http://localhost:8000`，控制面板 `http://localhost:8000/panel`。

<details>
<summary>Node.js 版本不对？</summary>

Vite 7 要求 Node.js **20.19+** 或 **22.12+**，推荐用 [nvm](https://github.com/nvm-sh/nvm) 管理：

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
nvm install 22 && nvm use 22
node -v   # 确认 >= 22.12
```

Windows 用户可用 [nvm-windows](https://github.com/coreybutler/nvm-windows) 或从 [Node.js 官网](https://nodejs.org/) 下载 LTS。
</details>

## 使用

### 1. 登录

首次使用需登录抖音，三选一：

<details open>
<summary><b>方式 A：本地浏览器扫码</b>（需 clone 仓库）</summary>

在宿主机运行（脚本依赖仓库内代码，仅源码部署可用），弹出真实浏览器窗口扫码，
登录态经 volume 自动同步到容器：

```bash
# 需先装 Playwright：pip install playwright && playwright install chromium
python3 login.py
```

扫码成功后浏览器自动关闭；检测到容器会自动重启使登录生效。
</details>

<details>
<summary><b>方式 B：Cookie 导入</b>（推荐镜像部署用户，最简单）</summary>

在任意浏览器登录抖音后导出 Cookie：

1. 打开 `douyin.com` 并登录
2. `F12` → **Application** → **Cookies** → `https://www.douyin.com`
3. 右键 Cookie 表格空白处 → **Copy all cookies**
4. 控制面板 `/panel` → **登录** → **导入 Cookie**，粘贴导入

支持 JSON 数组（DevTools Copy all cookies）和 `key=value; key=value` 字符串（`document.cookie`）两种格式。
</details>

<details>
<summary><b>方式 C：控制面板远程扫码</b></summary>

`/panel` → **登录** → **扫码登录**，通过截图远程操作容器内浏览器。适合临时使用，延迟较高。
</details>

登录态保存在 `data/browser_profile/`，经 volume 持久化。

### 2. 采集

在控制面板 **采集** 分区可视化操作（增量/全量切换、勾选会话、实时日志），或用命令行：

```bash
python3 extract.py                          # 全量采集所有会话
python3 extract.py --filter "会话名称"        # 只采集指定会话
python3 extract.py --filter "会话名称" --incremental   # 增量（只取新消息）
python3 extract.py --transcribe-voices      # 只补充本地数据库中的历史语音转写
```

### 3. 导出为 ChatLab 格式

导出 [ChatLab](https://github.com/hellodigua/ChatLab) 标准格式，可直接导入做 AI 分析：

```bash
python3 export.py --filter "会话名称"                    # JSONL（默认）
python3 export.py --filter "会话名称" --format json      # JSON
python3 export.py --filter "会话名称" --output data/export.jsonl
```

未指定 `--output` 时，文件会自动命名为 `会话昵称_YYYYMMDDHHMMSS_export.jsonl`（或 `.json`）；显式指定输出路径时保留用户给出的文件名。

导出内容：文本、表情、图片 URL、语音时长与转写文字、分享链接、引用/回复关系。也可在控制面板 **导出/导入** 分区一键操作。

新采集的语音会自动转写。对已经保存的历史消息，可在控制面板 **采集** 分区点击「补充历史语音转写」，或执行 `python3 extract.py --transcribe-voices`。任务只处理尚未完成的语音，不会重新采集全部聊天记录；缺少的发送者信息会在需要时自动补充。

控制面板还支持选择「整个数据库（SQLite，可导入）」导出完整的 `chat.db`，以及选择之前导出的 SQLite 文件「导入并覆盖数据库」。导入前会先校验数据库结构，并自动保留一份当前数据库备份；导入时请先停止正在运行的采集或媒体下载任务。

## 控制面板

访问 `/panel`，侧栏分区管理全部功能：

<div align="center">
  <img src="docs/control-panel.png" alt="控制面板" width="820">
</div>

| 分区 | 功能 |
|------|------|
| **概览** | 会话数 / 消息数 / 用户数 |
| **采集** | 刷新会话列表、增量/全量切换、全量风险确认、历史语音补充、图片/视频本地下载、实时日志 |
| **定时** | 标准 cron 表达式 + 预设快捷按钮 |
| **导出/导入** | 按会话导出 ChatLab JSON/JSONL；整库导出 SQLite；校验后覆盖导入并自动备份旧库 |
| **登录** | 远程扫码、Cookie 导入、检查/清除登录态 |
| **设置** | 访问密码、Server酱 失败通知；4 套主题、中英文切换 |

<details>
<summary><b>配置失败通知（Server酱）</b></summary>

适合开了定时任务的用户：cookie 失效、抖音接口变动导致采集失败时主动推送，免去定时查面板。

1. 到 [sct.ftqq.com](https://sct.ftqq.com) 用微信登录，复制 SendKey（形如 `SCT...`）
2. 控制面板 → **设置** → 通知 → 粘贴 SendKey → **设置**
3. 点 **测试** 验证微信能收到

后续每次采集失败（含定时任务）自动推送：

```
抖音聊天导出 · 采集失败
失败时间: 2026-05-26 18:42:11
原因: 采集失败 (exit code 2)
日志末尾:
  [+] 浏览器已启动
  [*] 等待扫码登录...
  [-] 未能登录，退出
```

</details>

## 注意事项

- 本工具仅用于导出**自己的**聊天记录备份，请勿用于非法用途
- 抖音可能随时更改接口导致工具失效
- 媒体 CDN URL 有签名有效期（约 1 年），过期后未本地化的图片/表情将无法显示
- 语音文件自动下载到 `data/media/voice/`，不受 CDN 过期影响
- 语音转写失败后可通过再次采集或「补充历史语音转写」重试
- 控制面板可开启「图片本地下载」，将图片和表情包持久化到 `data/media/`

## License

MIT
