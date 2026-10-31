# 北大法宝资料采集助手 · Enhanced

Python 3.10+ 桌面程序。浏览器手动登录 → 采集列表链接 → SQLite 排队 → 提取正文 → TXT / JSON / CSV。独立重写，参考 [hjhhoni/pkulaw 原程序](https://github.com/hjhhoni/pkulaw/blob/main/北大法典爬虫GUI.py) 的交互流程；原理与问题见 [ANALYSIS.md](ANALYSIS.md)。

## 安装运行（Windows PowerShell）

```powershell
cd pkulaw-enhanced
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
& 'C:\Program Files\Google\Chrome\Application\chrome.exe' --remote-debugging-address=127.0.0.1 --remote-debugging-port=9333 --user-data-dir="$PWD\browser-profile"
.\.venv\Scripts\python.exe crawler.py
```

使用已安装的 Chrome，通过 CDP 连接，无需下载 Playwright 浏览器。Chrome 路径按实际安装位置修改。独立浏览器配置目录是必要的；不要将调试端口暴露到公网。程序不读取或导出密码、Cookie。

1. 在新启动的 Chrome 中手动登录，打开目标检索结果页。
2. 设置数据目录，在“列表页 URL 匹配”填入能唯一匹配目标标签的 URL 片段。默认 `pkulaw`；多个匹配会报错，避免选错标签。
3. 默认列表链接选择器为 `tbody tr a[href]`、正文为 `.fulltext`、标题为 `h1`。这是参考原程序的初始配置，需根据实际页面检查调整。
4. 点击“采集链接”。默认仅当前页；自动分页需填写下一页 CSS，设置最大页数。下一页必须是 CSS 选择器；程序等待链接集合变化，重复页停止。
5. 点击“下载正文”。在同一浏览器上下文中新建专用标签，继承该上下文的登录状态。完成后只关闭专用标签。
6. 失败任务保留错误与尝试次数。“重新排队失败任务”后再次下载。登录、验证码、HTTP 401/403/429 提示会中止本批，当前任务仍待处理；手动处理后重新点击下载。

“导入 urls.txt”接受一行一个链接，相对链接按“导入链接基准 URL”解析；整个文件验证通过才写入，跨域链接拒绝。旧程序的 urls.txt 可直接导入（设置正确基准域名）。

## 增强功能

- SQLite 唯一 URL、持久状态、失败信息；成功任务重启后跳过，保持插入顺序。
- 有限指数退避重试；单批上限和最小请求间隔；采集和下载串行运行。
- 暂停/继续/停止；等待期间可及时停止。正在执行的浏览器调用需等待返回或超时，关闭窗口会等待工作线程退出。
- TXT 正文、JSON 来源/标题/采集时间/内容哈希、CSV 任务索引（UTF-8 BOM）。
- 文件名清理、URL 哈希避免同名覆盖、临时文件替换。只有 TXT 和 JSON 都写完才标记成功；进程崩溃后待办可重新下载。
- CSS 可配置、唯一标签选择、相对地址转换、拒绝跨域采集；Tkinter 标准库界面，通过队列传递工作线程日志。

数据保存在所选目录下：`tasks.sqlite3`、`index.csv`、`doc_标题_URL哈希.txt/json`。不要把浏览器配置目录或采集数据提交到 GitHub。一个数据目录同时只运行一个实例；已完成任务假定输出文件仍保留，删除文件后需要自行恢复备份或使用新的数据目录重新采集。

## 验证与限制

```powershell
python -m unittest -v
python -m py_compile crawler.py
```

自动化测试覆盖 URL 校验、同名文件隔离、数据库恢复、暂停时停止、访问提示中止、模拟超时重试及文件保存。测试不依赖联网和 Playwright。

未使用用户的北大法宝账号进行真实站点端到端测试。站点 DOM、登录机制与权限提示可能变化；正文长度检查也不能证明获得全文。首次请用少量记录核对输出。默认权限提示选择器只是起点，可补充实际站点提示容器。单页应用翻页与 iframe/虚拟列表可能需要适配。程序支持当前浏览器已可访问的资料整理，不实现验证码破解、付费限制绕过或反检测。

API 依据：[Playwright CDP 连接](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp)、[浏览器上下文](https://playwright.dev/python/docs/api/class-browsercontext)。
