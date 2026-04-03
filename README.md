# 抖音聊天记录导出工具

从抖音网页版完整导出私信聊天记录，并提供本地 Web 浏览界面。

## 功能

- **完整导出**：通过直接调用抖音 IM API（protobuf），突破虚拟列表滚动上限，可导出完整历史记录
- **精确排序**：使用服务端 `created_at_us` 单调递增序号排序，确保消息顺序正确
- **多种消息类型**：文本、表情包、图片、语音、分享视频/商品/直播、系统消息等
- **引用/回复消息**：提取引用消息数据，前端显示引用区块并支持点击跳转到原消息
- **语音消息**：自动下载语音文件到本地，前端支持播放
- **增量更新**：支持增量模式，只获取新消息
- **前端浏览器**：内置 Vue 3 + FastAPI 聊天记录浏览界面，支持无限滚动、全文搜索、搜索跳转
- **ChatLab 导出**：支持导出为 [ChatLab](https://github.com/hellodigua/ChatLab) 标准格式（JSON/JSONL），可用于 AI 聊天记录分析

## 安装

```bash
# 克隆项目
git clone https://github.com/TeamBreakerr/douyin-chat-export.git
cd douyin-chat-export

# 创建虚拟环境
python3 -m venv venv
source venv/bin/activate

# 安装 Python 依赖
pip install -r requirements.txt
playwright install chromium

# 安装前端依赖（如需开发模式）
cd frontend && npm install && cd ..
```

## 使用

### 1. 导出聊天记录

```bash
# 全量导出所有会话
python3 extract.py

# 只导出指定会话
python3 extract.py --filter "会话名称"

# 增量更新（只获取新消息）
python3 extract.py --filter "会话名称" --incremental
```

首次运行会打开 Chromium 浏览器，需要手动扫码登录抖音。登录状态会保存在 `data/browser_profile/` 中。

### 2. 浏览聊天记录

```bash
# 一键启动（后端 API + 前端静态文件）
./start.sh

# 或手动启动
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

访问 `http://localhost:8000` 查看聊天记录。

### 3. 导出为 ChatLab 格式

支持导出为 [ChatLab](https://github.com/hellodigua/ChatLab) 标准格式，可直接导入 ChatLab 进行 AI 分析。

```bash
# 导出为 JSONL（默认）
python3 export.py --filter "会话名称"

# 导出为 JSON
python3 export.py --filter "会话名称" --format json

# 指定输出路径
python3 export.py --filter "会话名称" --output data/export.jsonl
```

导出内容包括：文本、表情、图片 URL、语音（base64 嵌入）、分享链接、引用/回复关系。

## 注意事项

- 本工具仅用于导出**自己的**聊天记录备份，请勿用于非法用途
- 抖音可能随时更改 API 接口，导致工具失效
- 媒体 CDN URL 有签名有效期（约 1 年），过期后图片/表情包将无法显示
- 语音文件会下载到本地 `data/media/`，不受 CDN 过期影响

## License

MIT
