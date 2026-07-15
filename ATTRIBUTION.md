# Attribution / 归属说明

本项目基于 [TeamBreakerr/douyin-chat-export](https://github.com/TeamBreakerr/douyin-chat-export) 二次开发。

## 主要修改

- **修复 sessionid cookie 空值判断 bug**：原代码在检测 `sessionid` cookie 时仅检查名称存在，未验证 value 是否非空，导致 LevelDB 中空值 sessionid 被错误识别为有效，引发 0 条消息拉取问题
- **login.py cookie 刷新修复**：添加 5 秒等待确保 Chromium 将 sessionid 刷新到 LevelDB，并导出 JSON 备份
- **web_scraper.py JSON fallback**：当持久化 context 缺少有效 sessionid 时，自动从 `data/cookies_backup.json` 加载
- **Windows 本地部署指南**：Python venv + uvicorn，无需 Docker

## 原始项目

- 仓库：https://github.com/TeamBreakerr/douyin-chat-export
- 许可证：MIT License

---

*Forked and modified for personal use and bug fixes. All original copyrights belong to their respective owners.*
