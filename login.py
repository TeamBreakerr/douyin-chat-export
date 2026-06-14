#!/usr/bin/env python3
"""Open a local browser for Douyin QR login, then optionally sync to Docker."""
import asyncio
import os
import subprocess
import sys

from playwright.async_api import async_playwright

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(PROJECT_DIR, "data", "browser_profile")
DOUYIN_URL = "https://www.douyin.com/"


async def main():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    print("[*] 启动浏览器，请在弹出的窗口中扫码登录...")

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        args=["--disable-blink-features=AutomationControlled"],
    )
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )

    page = context.pages[0] if context.pages else await context.new_page()
    # Douyin's heavy JS / bot-detection can stall "domcontentloaded" well past
    # 30s even though the page is already usable for scanning. Wait only for the
    # navigation to commit, and don't let a slow load abort the login flow — the
    # QR UI renders client-side and the cookie poll below gives it 5 minutes.
    try:
        await page.goto(DOUYIN_URL, wait_until="commit", timeout=60000)
    except Exception as e:
        print(f"[!] 页面加载较慢，继续等待登录: {e}")

    print("[*] 等待登录... (登录成功后自动关闭)")
    for _ in range(300):  # 5 min timeout
        # Read the cookie jar instead of document.cookie: sessionid is httpOnly
        # (invisible to document.cookie) and context.cookies() also survives the
        # page navigations that destroy the JS execution context mid-poll.
        try:
            cookies = await context.cookies()
        except Exception:
            await asyncio.sleep(1)
            continue
        if any(c.get("name") == "sessionid" for c in cookies):
            print("[+] 登录成功！")
            break
        await asyncio.sleep(1)
    else:
        print("[-] 登录超时")
        await context.close()
        await pw.stop()
        return

    await context.close()
    await pw.stop()

    # Try to sync to Docker if running via Docker + OrbStack/volume
    sync_to_docker()


def _find_docker_container():
    """Find the running douyin-chat-export container, return name or None."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=douyin-chat-export",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=5,
        )
        name = result.stdout.strip()
        return name if name else None
    except Exception:
        return None


def _find_compose_data_mount():
    """Inspect container to find where ./data is mounted on the host."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "douyin-chat-export",
             "--format", "{{range .Mounts}}{{if eq .Destination \"/app/data\"}}{{.Source}}{{end}}{{end}}"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def sync_to_docker():
    """Detect Docker environment and sync browser_profile."""
    container = _find_docker_container()
    if not container:
        # Try via OrbStack
        try:
            result = subprocess.run(
                ["orb", "run", "docker", "ps", "--filter", "name=douyin-chat-export",
                 "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=5,
            )
            if not result.stdout.strip():
                print("[*] 未检测到 Docker 容器，登录态已保存到 data/browser_profile/")
                return
        except (FileNotFoundError, subprocess.CalledProcessError):
            print("[*] 未检测到 Docker 环境，登录态已保存到 data/browser_profile/")
            return

        # OrbStack mode: find mount path and sync
        _sync_via_orbstack()
        return

    # Local Docker mode: find mount path
    mount_path = _find_compose_data_mount()
    if mount_path:
        target = os.path.join(mount_path, "browser_profile")
        if os.path.abspath(target) != os.path.abspath(PROFILE_DIR):
            print(f"[*] 同步到 Docker volume: {target}")
            subprocess.run(["rm", "-rf", target])
            subprocess.run(["cp", "-a", PROFILE_DIR, target])
            subprocess.run(["docker", "restart", container])
            print("[+] 同步完成，容器已重启")
        else:
            print("[+] Docker volume 直接挂载项目 data/，无需同步")
    else:
        print("[*] 无法检测 Docker volume 挂载路径，请手动同步 data/browser_profile/")


def _sync_via_orbstack():
    """Sync profile via OrbStack (macOS host → Linux VM)."""
    print("[*] 检测到 OrbStack 环境，正在同步...")
    try:
        # Find mount path inside OrbStack
        result = subprocess.run(
            ["orb", "run", "docker", "inspect", "douyin-chat-export",
             "--format", "{{range .Mounts}}{{if eq .Destination \"/app/data\"}}{{.Source}}{{end}}{{end}}"],
            capture_output=True, text=True, timeout=5,
        )
        mount_path = result.stdout.strip()
        if not mount_path:
            print("[-] 无法检测 Docker data 挂载路径")
            return

        target = f"{mount_path}/browser_profile"
        # macOS path accessible from OrbStack Linux via /mnt/mac
        subprocess.run(["orb", "run", "sudo", "rm", "-rf", target], capture_output=True)
        subprocess.run(
            ["orb", "run", "sudo", "cp", "-a", f"/mnt/mac{PROFILE_DIR}", target],
            check=True,
        )
        # Fix ownership to match container expectations
        subprocess.run(
            ["orb", "run", "sudo", "chown", "-R", "1000:1000", target],
            capture_output=True,
        )
        subprocess.run(["orb", "run", "docker", "restart", "douyin-chat-export"], check=True)
        print("[+] 同步完成，容器已重启")
    except subprocess.CalledProcessError as e:
        print(f"[-] 同步失败: {e}")
        print("[*] 请手动复制 data/browser_profile/ 到 Docker volume")


if __name__ == "__main__":
    asyncio.run(main())
