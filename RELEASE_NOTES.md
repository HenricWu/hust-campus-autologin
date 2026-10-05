## v1.3.2：公开下载与作者署名

仓库及安装包现已开放访问，无需登录 GitHub 即可下载。本版在软件底部加入作者、邮箱、GitHub 项目入口及版权声明：

**© 2026 HenricWu. All rights reserved.**

## 下载哪个文件

在页面下方 **Assets** 中下载 **`HUST-Connect-v1.3.2-Windows.zip`**，右键选择 **全部解压**。

**运行前需要：Windows 10/11 + Python 3.10 或更新版本（含 Tkinter），安装需要管理员权限。** Python 安装时请配置 PATH；无需额外安装第三方 Python 包。

## 第一次使用

1. 未安装 Python 时，先从 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/) 安装完整版，保留 Tcl/Tk 组件。
2. 打开解压后的 `CampusAutoLogin` 文件夹，右键 **`一键安装.cmd`**，选择 **以管理员身份运行**。
3. 在 Windows 管理员权限提示中选择 **是**，等待设置窗口打开。
4. 填写校园网账号、密码，选择接入校园网的网卡。**服务名称通常留空**。
5. 点击 **一键登录**，确认显示 **校园网在线** 或 **连接已恢复**。
6. 确认 **每分钟自动检测** 开关已打开，最近检测时间约每分钟更新。

安装完成后使用桌面快捷方式 **华中科技大学校园网自动登录**。关闭窗口不会停止后台任务，日常使用不需要终端。

完整图文步骤、安装失败处理和常见问题见 [README](https://github.com/HenricWu/hust-campus-autologin#readme)。

## 这一版包含什么

- 简洁白色界面、浅灰分区、深灰按钮和官网高清彩色校徽。
- 一键登录、每 60 秒检测及掉线后按需重登。
- 开机后台运行，锁屏或远程控制断开后继续检测。
- 本机加密保存凭据；发布包不包含个人账号密码。
- 作者 **HenricWu**，联系邮箱 **haoyangwu@hust.edu.cn**。

## 升级和卸载

**升级：**关闭旧设置窗口，解压新版本，再右键 `一键安装.cmd` 选择 **以管理员身份运行**。同一 Windows 用户的已保存设置会保留。

**卸载：**在解压目录打开管理员 PowerShell，执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

加上 `-ForgetCredentials` 可同时清除已保存的加密账号。卸载不会注销当前校园网连接。

## 使用范围与验证

- 当前适配 `http://172.18.18.60:8080` 的 HUST 锐捷 ePortal；其他入口需要单独适配。
- 电脑必须开机并接入校园网。关机、睡眠、验证码、账号欠费或校园网服务停机不能由本工具自动解决。
- 正常情况下每分钟检测；认证服务器暂时拒绝时，登录提交会逐步延长重试间隔。
- 20 项核心测试、模拟重连、界面按钮联动及本机在线检测已通过。真实月初失效和重启后的完整恢复链路仍需实际运行验证。

作者与版权声明见 [NOTICE.md](https://github.com/HenricWu/hust-campus-autologin/blob/main/NOTICE.md)。

### 可选：核对下载文件

下载 `HUST-Connect-v1.3.2-SHA256.txt`，在安装包所在目录运行：

```powershell
Get-FileHash .\HUST-Connect-v1.3.2-Windows.zip -Algorithm SHA256
```

将结果与校验文件比较即可。
