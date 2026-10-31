# 票夹 · QQ 邮箱发票整理

提供网页版试用与原有 Windows 本机版：从 QQ 邮箱中查找指定日期范围内的发票邮件，自动保存 PDF 附件和可直接下载的发票 PDF。

原本机版只使用 Python 标准库；网页版由服务器运行，不要求访客安装任何依赖。邮箱与 IMAP 授权码仅用于本次整理，不写入配置文件。

## 网页试用版

[打开票夹在线试用](https://piao-jia-jh.onrender.com)。无需安装软件。

网页采用「填写邮箱 → 选择日期 → 下载发票」三步流程，无需安装软件。提供 QQ 官方授权码获取指南、无需连接邮箱的示例体验、ZIP 打包下载和主动清除。

每位访客的任务独立，授权码只在任务处理期间保存在服务端内存。生成的文件最多保留 15 分钟，超时或主动清除后不可下载。只读收件箱，不发送、删除或标为已读。网页版使用 Flask / Gunicorn；原本机版仍仅依赖 Python 标准库。

运行、部署和数据边界见 [网页部署说明](docs/public-trial-deployment.md)，设计范围见 [网页设计](docs/public-trial-design.md)。

## 原本机版下载和使用

1. 点击 GitHub 页面右上方的 `Code` → `Download ZIP`。
2. 解压下载的 ZIP。
3. 打开其中的 `发票下载工具` 文件夹。
4. 双击 `双击此文件开始下载.bat`。
5. 浏览器会自动打开 `http://127.0.0.1:8765`。
6. 填写 QQ 邮箱、IMAP 授权码、起始日期和截止日期，然后点击“开始下载”。
7. 下载完成后，在 `发票下载工具/downloads` 中查看 PDF。

需要 Windows 10/11 和 Python 3.7 或以上版本。如果电脑没有 Python，请从 [Python 官网](https://www.python.org/downloads/windows/)下载安装，并在安装时勾选 `Add python.exe to PATH`。

## QQ 邮箱授权码

IMAP 授权码不是 QQ 登录密码。

登录 [QQ 邮箱](https://mail.qq.com/)，进入邮箱设置中与 POP3/IMAP 服务相关的页面，开启 IMAP/SMTP 服务，并按页面提示生成授权码。请像保管密码一样保管授权码，不要通过聊天、邮件或截图发送给他人。

## 项目结构

```text
qq-invoice-downloader/
├── README.md
└── 发票下载工具/
    ├── 代码/
    │   ├── download_invoices.py
    │   ├── invoice_web_app.py
    │   ├── safe_download.py
    │   ├── public_app.py / public_jobs.py / web/（网页版）
    │   └── README.md
    └── 双击此文件开始下载.bat
```

`downloads/` 不会预先创建，只会在第一次成功保存 PDF 时自动生成。

## 下载规则

- 只扫描 QQ 邮箱收件箱 `INBOX`。
- 日期范围包含起始日期和截止日期当天。
- 只处理主题、发件人或正文中包含“发票”的邮件。
- 邮件附件只保存 PDF，跳过 XML、OFD、ZIP、CSV 等附件。
- 正文链接直接返回 PDF 时保存 PDF；返回 ZIP 时只提取其中的 PDF。
- 登录页、扫码页、短信验证页或其他无法自动处理的链接，会记录到 `需要手动下载的发票链接.txt`。
- 同名 PDF 已存在时直接跳过，不覆盖也不创建重复副本。
- 所有 PDF 都保存在同一个 `downloads/` 文件夹中。

## 本机版隐私与安全

- 网页服务只监听本机地址 `127.0.0.1:8765`。
- 不使用 `.env`，不创建 `download_state.json`。
- 授权码不会出现在网页状态、运行日志或下载文件中。
- 自动下载会限制响应大小，并拒绝访问本机或内网地址。
- 请勿公开上传 `downloads/`、`需要手动下载的发票链接.txt` 或任何个人发票。

更完整的使用方法和常见问题请查看：[详细中文说明](发票下载工具/代码/README.md)。
