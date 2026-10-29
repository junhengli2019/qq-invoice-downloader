# 票夹 Web 版

把原 QQ Windows 工具扩展成手机、电脑浏览器可用的私有网站。原 `发票下载工具/` 原封不动保留；Web 版代码位于 `web/`，采用独立的安全服务端实现。

## 首版支持

- QQ（qq.com / vip.qq.com / foxmail.com）、163.com，固定 IMAP TLS 993 服务端地址，不允许用户自定义主机。
- 收件箱 INBOX、含首尾日期的日期范围、主题/发件人/文本正文关键词筛选（默认“发票”）。日期按 IMAP 收件内部日期筛选，不是附件发票开票日期。
- 原始 PDF 附件检索、重复内容去重、多选后打包 ZIP 下载到当前设备；手机由浏览器下载/共享系统保存。
- 最多 366 天、最新 300 封收件箱邮件、100 个 PDF、合计 48 MiB；超大邮件跳过并计数。
- 网站访问口令保护、CSRF、会话隔离、HTTPS、过期清理及手动清除。

**明确不支持：**邮件正文链接的自动访问、附件 ZIP/OFD/XML 自动提取、非收件箱文件夹、验证码/登录页解析、其他邮箱服务商。本版本不跟随任何邮件 URL，以避免恶意链接/内网访问风险。页面展示仅有链接且没有可用 PDF 的匹配邮件数量，用户应回原邮箱手动检查。PDF 文件名/邮件关键词并不能保证附件一定是真实发票，请自行核验。

## 隐私边界

这不是浏览器直连 IMAP 的静态网页。邮箱地址与授权码经 HTTPS 发送到部署服务器，服务器仅在本次请求内存中用于登录邮箱；不写入配置、数据库或应用日志。结果和 PDF 在服务器内存中最多保留 15 分钟，清除/退出立即移除应用引用，后台每 30 秒清理到期结果。服务重启会丢失所有结果，会话最长 1 小时。

应用以只读 EXAMINE + BODY.PEEK[] 读取邮件，不发送、不删除、不改变已读标记；邮箱提供的授权码本身可能拥有更广泛的权限，不能称之为“只读授权码”。Python 无法保证内存物理擦除；应只部署到自己信任的服务器，禁用请求体/APM 日志，不启用 debug/core dump，谨慎设置基础设施日志与内存转储。

## 本地验证

需要 Python 3.12：

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
# 在部署平台秘密环境变量或当前交互 shell 中设置 APP_ACCESS_CODE（至少16字符）。
# 不要提交到 Git，不要把邮箱授权码配置成环境变量。
gunicorn --bind 127.0.0.1:8080 --workers 1 --threads 4 --timeout 210 web.app:app
```

APP_ACCESS_CODE 未配置或过短时，网页可展示，但邮箱连接端点保持锁定。测试只使用虚拟消息和模拟 IMAP，**没有验证真实 QQ/163 授权码或生产网络可达性**。

## Docker / Render 部署

`Dockerfile` 使用非 root 用户、一个进程四个线程，监听 `PORT`（默认8080），`/healthz` 健康检查。`render.yaml` 描述一个免费 Docker Web Service，不自动生成或保存访问口令，由站点所有者在 Render 的 Secret Environment 设置 APP_ACCESS_CODE。创建资源前确认当前服务套餐及任何费用。

Render 设置：

- Runtime: Docker；仓库根目录的 Dockerfile。
- Plan: Free（可用时；休眠/唤醒会使结果丢失；不要自动升级付费）。
- Environment: `APP_ACCESS_CODE` 至少16个字符的独立强口令；`TRUST_PROXY=1`。
- Health check: `/healthz`。
- 访问 Render 提供的 HTTPS 地址；不要使用裸 HTTP 传输授权码。
- 云平台必须允许到 `imap.qq.com:993` 和 `imap.163.com:993` 的出站 TCP/TLS。Cloudflare Workers/Sites 的仅 HTTP 运行时不适用。

`TRUST_PROXY=1` 仅适用于无法绕过的可信反向代理，应用只信任最后一跳的 X-Forwarded-Proto 和 X-Forwarded-For。若容器直接暴露公网而没有可信代理，保持该变量未设置并在 TLS 入口终止 HTTPS。不要自行开启 CORS。

**只运行一个 Gunicorn worker，且不要横向扩容。**结果与会话是内存存储，不同进程不共享。此首版面向个人/小范围信任用户，不是公开多租户 SaaS；最多200个活跃会话（未登录会话 5 分钟过期，满额时仅淘汰最旧未登录会话；已登录结果不会因匿名请求而被清除），两次并行扫描，结果内存总量96MiB（解析和生成ZIP期间会另占内存）。大规模部署需增加身份体系、共享任务队列和加密存储，并重新评估威胁模型。

## 邮箱开通

- QQ：邮箱设置 → 账号与安全/安全设置 → 开启 IMAP/SMTP，生成授权码。详情：https://help.mail.qq.com/detail/106/985
- 163：邮箱设置 → POP3/SMTP/IMAP → 开启 IMAP/SMTP → 新增授权密码。操作参考：https://help.aliyun.com/zh/model-studio/connector/netease-mail
- 登录邮箱网页完成首次客户端安全验证；不要在本站输入正常邮箱登录密码。用完可在邮箱网页撤销授权码。

## 部署验收

1. HTTPS 可访问，未配置访问口令时无法扫描。
2. 正确/错误口令、CSRF校验、退出与刷新状态均正常。
3. 用户在安全页面自行输入测试邮箱授权码，选少量已知 PDF 邮件测试；不得在聊天中发送授权码。
4. QQ 和 163 分别检验扫描结果、单选/多选ZIP、手机保存、未读邮件仍未读。
5. 15分钟后下载返回过期；不同浏览器会话不能下载彼此结果。
6. 检查托管日志不含邮件正文、授权码或下载内容。

真实邮箱联调和部署后的网络验证必须完成后才能宣称全部上线可用。
