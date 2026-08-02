# open-free-router · 模力自由港

这是 `open-free-router` Python CLI 的 npm 安装入口。它不会重写路由器：首次运行时会检查 Python 3.11+，在用户缓存目录创建隔离虚拟环境，并安装 npm 包内随附的同版本 Python 源码。

```bash
npm install -g open-free-router
open-free-router serve
# 短命令也可用
ofr doctor
```

要求：Node.js 18+、Python 3.11+。API Key 仍只由本机 `open-free-router setup` 或环境变量管理，npm 引导器不会读取或上传凭据。

- 网站：https://oaf.asia/
- 安装指南：https://oaf.asia/guide/
- 源代码：https://github.com/zhanglunet/open-free-router
- 原始项目：https://github.com/NoelJudeNoel/open-free-router
