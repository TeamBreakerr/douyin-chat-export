# 预构建镜像只发布到 GHCR，不做国内 registry 双推

本地构建镜像要依次穿过 Docker Hub、npm、PyPI、Playwright CDN、Debian apt 五个网络，国内用户很容易在其中一环失败（#28）。改为 CI 构建（amd64/arm64 原生 runner）并只推送 ghcr.io：public 仓库免费、`GITHUB_TOKEN` 即可推送、零 secret 配置。国内拉取 ghcr.io 不畅由南京大学镜像代理解决（`ghcr.io` → `ghcr.nju.edu.cn` 前缀替换）。

## Considered Options

- **双推阿里云 ACR**：国内拉取体验最好，但需要注册阿里云、往 GitHub 配 AccessKey secret，且 ~2GB 镜像双推让 CI 时间翻倍。若南大公益代理停服，再重新考虑。
- **Docker Hub**：国内加速器正被陆续下架（2026-06 上海交大源因监管下架），可靠性反而不如 GHCR + 南大代理，且有匿名拉取限速。

## Consequences

- 标签策略：push main → `latest` + `main-<sha>`；`v*` tag → `X.Y.Z` / `X.Y` / `X`。想稳的用户 pin 版本号，想快的跟 `latest`。
- 发布以 CI 通过为前提（`needs: [tests, frontend]`），但 CI 是离线特征测试，挡不住抖音接口变动导致的抓取回归——`latest` 用户（尤其挂 watchtower 的）仍可能被推到抓取已坏的版本，回滚手段是 pin `main-<sha>` 或版本号。
