# 影石助手 · Insta360 X5 Assistant

通过 Wi-Fi 在 Mac 上访问 Insta360 X5 素材的实验性原生应用。提供 Finder 只读素材盘、按需缓存、实时画面预览和开始／停止录像按钮。

**非影石官方软件。当前版本 0.4.1；尚未完成该版本在真实 X5 上的 Studio 导出与持续预览复测，请勿将其视为已经达到有线连接的稳定性。**

## 使用方式

1. 在 X5 的设置中打开 Wi-Fi，查看热点名称与密码。
2. 在 Mac 的 Wi-Fi 菜单中手动连接相机热点，并断开手机与相机的连接。相机热点通常不能提供互联网访问。
3. 打开「影石助手」，点击连接相机。首次挂载时，macOS 可能要求管理员授权。
4. 从 Finder 打开素材盘，或在 Insta360 Studio 中导入盘内素材。
5. 将成品导出到 **Mac 本地文件夹**。素材盘只读，不能作为导出目的地。
6. 保存编辑并关闭正在使用素材的应用，再在助手中断开连接。

无需提前完整下载原素材，但读取、预览和导出仍会通过 Wi-Fi 传输所需数据。采用 1 MiB 分块，默认最多保留约 128 MiB 内存缓存及 2 GiB 临时磁盘缓存；正常断开后清理缓存。完整导出可能读取整个源文件，缓存不会消除网络带宽限制。

## 功能与限制

- 分页发现相机素材，校验 HTTP Range 与实际文件长度；保留原素材及 LRV 预览文件。
- 使用 macOS 原生 NFS 客户端挂载只读素材盘，不需要额外安装 FUSE 驱动。
- 实时预览为实验性 H.264 解码画面，最多约 10 fps、最长边 960 像素；目前没有全景拼接、视角重构或音频播放。
- 开始／停止录像沿用相机当前视频设置，录像保存在相机中。新素材需要重新连接后刷新列表。
- 素材仍在使用时保留挂载服务，提示关闭素材后重试断开。
- 相机必须保持开机和 Wi-Fi 连接；未缓存的数据在相机离线后无法读取。
- 当前构建目标为 Apple Silicon Mac，Swift 最低部署目标为 macOS 14；尚未完成各 macOS 版本兼容性测试，也未提供 Intel 构建。
- 社区相机协议可能随固件变化。素材发现不保证覆盖所有拍摄模式或所有固件。

0.4.1 修复了 HTTP 长连接响应残留可能造成的目录缺项与读取失败，并增加读请求重试和范围校验。该修复已通过模拟相机及 macOS 挂载测试，实际 Studio 导出结果仍需复测。详情见 [验证记录](docs/validation.md)。

## 从源码构建

需要 Apple Silicon Mac、Xcode Command Line Tools，以及可创建虚拟环境的 Python。当前验证环境使用 Python 3.14；依赖版本见 `requirements.txt`。

```sh
git clone https://github.com/zhaoyu190/insta360-x5-assistant.git
cd insta360-x5-assistant
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py' -v
python scripts/build.py
open dist/影石助手.app
```

构建结果位于 `dist/`：应用及 ZIP 包。脚本使用本地 ad-hoc 签名，没有 Apple 开发者签名或公证；分发到其他电脑时可能触发 Gatekeeper 检查。构建不会启动应用或连接相机。

### 可选原生挂载测试

以下测试只访问本地模拟服务器，但会实际创建和卸载 macOS NFS 挂载。先断开助手的素材盘，再执行：

```sh
YINGSHI_RUN_NFS_TESTS=1 python tests/native_mount_check.py
YINGSHI_RUN_NFS_TESTS=1 python tests/native_lifecycle_check.py
```

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `app/` | SwiftUI 界面及应用配置 |
| `backend/camera_io.py` | 目录发现、HTTP Range 读取与缓存 |
| `backend/bridge.py` | 本地只读 NFS v3 服务 |
| `backend/live_camera.py` | 相机实时预览协议、视频解码和录像控制 |
| `backend/app_backend.py` | 挂载及后端生命周期 |
| `scripts/` | 构建脚本与自绘应用图标 |
| `tests/` | 模拟相机与原生挂载验证 |

更多实现说明见 [架构](docs/architecture.md)。提交问题时请附系统、固件版本和复现步骤，并先删除日志中的素材名称、路径及其他私人信息。

## 开源许可

本项目采用 **GPL-3.0-or-later**，完整条款见 [LICENSE](LICENSE)。依赖和协议参考见 [NOTICE](NOTICE)。Insta360、影石及相关产品名称属于其各自权利人；本项目与影石官方没有隶属或背书关系。
