# 网页试用版运行与验证

## 运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/gunicorn -c gunicorn.conf.py 'public_app:create_app()'
```

默认端口 8766。线上使用 Render 的 `PORT` 与 `RENDER_EXTERNAL_HOSTNAME`，自动确定受信任域名、HTTPS Cookie 和请求来源。自托管必须设置准确的 `PUBLIC_ORIGIN`，例如 `https://invoice.example.com`，并在前端终止 HTTPS。不要启用开发调试器。

应用包含进程内会话和任务管理，**必须为单实例、一个 Gunicorn Web worker**；其 8 个请求线程与最多 2 个独立整理子进程负责并发。勿直接增加 Web worker 或实例数量。未来扩容需要先引入持久化任务队列、共享会话与存储，不属于本次小范围试用。

## Render 配置

- 类型：Web service；runtime：Python；region：Singapore。
- 方案：Free，复用用户确认的工作区。不新增数据库或持久磁盘。
- 构建：`pip install -r requirements.txt`。
- 启动：`gunicorn -c gunicorn.conf.py 'public_app:create_app()'`。
- 健康检查：`/healthz` 返回当前应用版本及启动时的 QQ TLS 连通结果。就绪本身不依赖 QQ；临时外部网络故障不会反复重启网站。
- 参考：https://render.com/docs/web-services 、https://render.com/docs/free 。免费服务闲置 15 分钟后会休眠，首次唤醒可能约一分钟；文件与任务不持久化，不适合大规模或高可用生产使用。带宽和构建有平台额度，具体以账号账单设置为准。

## 数据边界

访客 Cookie 是随机的 256 位能力标识，HttpOnly / SameSite=Lax / 线上 Secure；浏览器不保存邮箱凭证到 localStorage、sessionStorage 或 Cookie。每次写操作必须带对应 CSRF 值并且 Origin 与网站域名相同。文件访问不接受可猜测的路径或任意 job ID，只以当前会话找出对应文件。

邮箱及授权码经 HTTPS 送到服务端，作为子进程运行参数存于内存，不放入命令行、环境变量、磁盘、网页状态或应用日志。整理子进程完成/停止/超时后退出；原始邮件仅在处理过程中读入内存。提取的 PDF 和链接清单暂存于独立 0700 目录，完成后 15 分钟到期，清理线程每 2 秒检查一次；可以立即删除。失败任务的部分文件不提供下载且立即清理。重启会清理专用临时目录。一次已经开始的下载可以在服务器清理文件后继续完成。

使用只读 INBOX 与 BODY.PEEK，不发送、删除或标为已读。QQ 授权码本身可能拥有读写/发送权限；界面说明本工具的实际行为，不声称这是 QQ 提供的只读授权。

试用限制：一次最多 31 天、300 封邮件、单封 12 MB、100 份 PDF、40 MB 输出、80 个正文链接、180 秒处理。最多 2 项同时运行，20 份待清理结果，保留空间总预算 240 MB（启动时为在运行任务预留空间）。每网络地址每小时 8 次开始操作，全站每小时 60 次；示例也计入。额度仅在当前进程生效，重启会重置，不宣称为永久防滥用机制。

自动下载逐次验证公开 DNS 地址，直接连接已验证的地址，并保留 TLS 证书与主机名校验，禁止内网/本机/组播地址、非 HTTP(S) 协议、非 80/443 端口及无限跳转。各类输入有大小限制，ZIP 展开有尺寸上限，最终文件名会清理路径字符。

## 验证方式

```sh
.venv/bin/python -m unittest discover -s tests -v
python3 '发票下载工具/代码/download_invoices.py' --self-test
```

11 项单元/集成测试：模拟 IMAP 邮件 → 原下载核心 → ZIP；两访客隔离；Cookie、同源和 CSRF；Host 与大小限制；授权失败；过期删除；中断子进程；全站并发；只读与 PEEK；内网与重定向；DNS 固定连接；大邮件提前拒绝。

浏览器检查用 Python Playwright + 本机 Chrome，先启动上述 8766 服务和 `tests/serve_fixture.py` 的 8767 模拟邮箱服务，再执行 `python3 tests/browser_flow.py`。模拟邮箱服务只绑定本机且不被生产应用导入。检查真实表单操作、授权失败恢复、ZIP 下载、刷新、两浏览器隔离、删除、320/390px 手机页面、键盘关闭指南、浏览器脚本错误。

本地已验证到 QQ 官方 IMAP 的 TLS 连接。浏览器完整收信流程使用的是合成邮件，不能当作真实邮箱认证的证据。发布后还须检查公网及云端 QQ 连通性；首次真实账号验收应由用户在网页里自行填入授权码，不通过聊天或代码传递。

## 授权引导来源

2026-10-31 核对 [QQ 官方帮助](https://help.mail.qq.com/detail/106/985)：网页版右上角头像 → 设置 → 账号与安全 → 安全设置，开启服务、生成 16 位授权码；失效操作在设备管理/授权码管理。QQ 页面可能调整，指南保留官方链接和旧版入口说明。
