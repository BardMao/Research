"""Browser-assisted document collector. Python 3.10+, no credentials stored."""
from __future__ import annotations

import csv
import hashlib
import json
import queue
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


def normalize_url(value, base):
    p = urlsplit(urljoin(base, value.strip()))
    origin = urlsplit(base)
    if p.scheme not in ('http', 'https') or p.username or p.password:
        raise ValueError('仅接受 HTTP(S) 链接')
    if (p.scheme, p.netloc.lower()) != (origin.scheme, origin.netloc.lower()):
        raise ValueError('跳过跨域链接')
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or '/', p.query, ''))


def filename(title, url):
    title = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', title).strip(' .')[:80]
    return 'doc_' + (title or 'untitled') + '_' + hashlib.sha256(url.encode()).hexdigest()[:16]


def atomic_write(path, text):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(path)


class Store:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.folder / 'tasks.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS tasks (
            url TEXT PRIMARY KEY, title TEXT DEFAULT '', status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0, error TEXT DEFAULT '', file TEXT DEFAULT '',
            updated TEXT DEFAULT CURRENT_TIMESTAMP)''')
        self.db.commit()

    def close(self):
        self.db.close()

    def add(self, urls):
        before = self.db.total_changes
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO tasks(url) VALUES (?)', ((u,) for u in urls))
        return self.db.total_changes - before

    def pending(self):
        return self.db.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY rowid").fetchall()

    def record(self, url, status, title='', error='', file=''):
        with self.db:
            self.db.execute('''UPDATE tasks SET status=?,title=?,error=?,file=?,
                attempts=attempts+1,updated=CURRENT_TIMESTAMP WHERE url=?''',
                (status, title, error, file, url))

    def retry_failed(self):
        with self.db:
            self.db.execute("UPDATE tasks SET status='pending',error='' WHERE status='failed'")

    def export(self):
        rows = self.db.execute('SELECT * FROM tasks ORDER BY rowid').fetchall()
        with (self.folder / 'index.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['url', 'title', 'status', 'attempts', 'error', 'file', 'updated'])
            # Protect spreadsheet users from formula interpretation.
            writer.writerows([("'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v)
                              for v in row] for row in rows)
        return dict(self.db.execute('SELECT status,count(*) FROM tasks GROUP BY status').fetchall())


class Cancelled(Exception):
    pass


class AccessRequired(Exception):
    pass


class Control:
    def __init__(self):
        self.stop = threading.Event()
        self.paused = threading.Event()

    def check(self):
        while self.paused.is_set():
            if self.stop.wait(.1):
                raise Cancelled()
        if self.stop.is_set():
            raise Cancelled()

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check()
            if self.stop.wait(min(.1, max(0, end - time.monotonic()))):
                raise Cancelled()
        self.check()


class Collector:
    def __init__(self, settings, control, emit):
        self.s, self.control, self.emit = settings, control, emit

    def guarded(self, page, response=None):
        if response and response.status in (401, 403, 429):
            raise AccessRequired(f'HTTP {response.status}：请手动确认访问权限或稍后再试')
        if re.search(r'/(login|signin)(/|\?|$)', page.url, re.I):
            raise AccessRequired('已跳转登录页，请在浏览器手动登录')
        if page.locator(self.s['guard']).count():
            raise AccessRequired('检测到登录、验证码或权限提示，请手动处理后继续')

    def run(self, mode):
        with closing(Store(self.s['folder'])) as store:
            try:
                if mode == 'retry':
                    store.retry_failed()
                    return
                if mode == 'export':
                    return
                if mode == 'import':
                    base = self.s['base']
                    urls = []
                    for line in Path(self.s['import_file']).read_text(encoding='utf-8-sig').splitlines():
                        if line.strip():
                            urls.append(normalize_url(line, base))
                    self.emit(f'导入新增 {store.add(urls)} 条')
                    return
                from playwright.sync_api import sync_playwright
                # Driver lifetime ends here; do not close the attached browser/context.
                with sync_playwright() as p:
                    browser = p.chromium.connect_over_cdp(self.s['endpoint'], timeout=15000)
                    pages = [page for context in browser.contexts for page in context.pages
                             if page.url.startswith(('https://', 'http://'))]
                    if not pages:
                        raise ValueError('浏览器中没有网页，请先打开检索结果页')
                    matches = [page for page in pages if self.s['tab_match'] in page.url]
                    if len(matches) != 1:
                        raise ValueError('标签匹配结果必须恰好为一个，请填写更完整的列表页 URL')
                    source = matches[0]
                    source.set_default_timeout(self.s['timeout'] * 1000)
                    if mode == 'collect':
                        self.collect(source, store)
                    else:
                        detail = source.context.new_page()
                        detail.set_default_timeout(self.s['timeout'] * 1000)
                        try:
                            self.download(detail, store)
                        finally:
                            detail.close()
            finally:
                self.emit('任务统计：' + json.dumps(store.export(), ensure_ascii=False))

    def collect(self, page, store):
        seen = set()
        for index in range(self.s['pages']):
            self.control.check()
            self.guarded(page)
            page.locator(self.s['links']).first.wait_for(state='attached')
            hrefs = page.locator(self.s['links']).evaluate_all('(els) => els.map(e => e.href)')
            signature = tuple(hrefs)
            if signature in seen:
                self.emit('列表重复，已停止翻页')
                return
            seen.add(signature)
            urls = []
            for href in hrefs:
                try:
                    urls.append(normalize_url(href, page.url))
                except ValueError:
                    pass
            self.emit(f'第 {index + 1} 页新增 {store.add(urls)} 条')
            if index + 1 >= self.s['pages'] or not self.s['next']:
                return
            button = page.locator(self.s['next']).first
            if not button.count() or not button.is_visible() or not button.is_enabled():
                return
            if button.get_attribute('aria-disabled') == 'true' or 'disabled' in (button.get_attribute('class') or ''):
                return
            self.control.wait(self.s['interval'])
            button.click()
            page.wait_for_function('''({selector, old}) => {
                const links = [...document.querySelectorAll(selector)].map(e => e.href);
                return links.length > 0 && JSON.stringify(links) !== JSON.stringify(old);
            }''', arg={'selector': self.s['links'], 'old': hrefs})

    def download(self, page, store):
        tasks = store.pending()[:self.s['batch']]
        for index, task in enumerate(tasks):
            url = task['url']
            for attempt in range(self.s['retries'] + 1):
                self.control.wait(self.s['interval'] * (2 ** attempt))
                try:
                    response = page.goto(url, wait_until='domcontentloaded')
                    self.guarded(page, response)
                    if response and response.status >= 400:
                        raise RuntimeError(f'HTTP {response.status}')
                    content = page.locator(self.s['content']).first
                    content.wait_for(state='visible')
                    self.guarded(page)
                    text = content.inner_text().strip()
                    if len(text) < self.s['min_chars']:
                        raise ValueError('正文过短，未保存；请检查权限和正文选择器')
                    title_node = page.locator(self.s['title']).first
                    title = title_node.inner_text().strip() if title_node.count() else page.title()
                    title = title or text.splitlines()[0]
                    stem = filename(title, url)
                    self.control.check()
                    atomic_write(store.folder / (stem + '.txt'), text)
                    atomic_write(store.folder / (stem + '.json'), json.dumps({
                        'url': url, 'title': title, 'text': text,
                        'collected_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                        'sha256': hashlib.sha256(text.encode()).hexdigest()
                    }, ensure_ascii=False, indent=2))
                    store.record(url, 'done', title=title, file=stem + '.txt')
                    self.emit(f'[{index + 1}/{len(tasks)}] 已保存：{title[:100]}')
                    break
                except (Cancelled, AccessRequired):
                    raise
                except Exception as exc:
                    final = attempt == self.s['retries']
                    store.record(url, 'failed' if final else 'pending', error=str(exc)[:1000])
                    self.emit(f'[{index + 1}/{len(tasks)}] 第 {attempt + 1} 次尝试失败：{str(exc)[:200]}')
                    if final:
                        break


def launch_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    root = tk.Tk()
    root.title('北大法宝资料采集助手 · Enhanced')
    root.geometry('920x800')
    events = queue.Queue()
    worker = None
    control = Control()
    defaults = {
        'folder': str(Path('data').resolve()), 'endpoint': 'http://127.0.0.1:9333',
        'tab_match': 'pkulaw', 'base': 'https://www.pkulaw.com/',
        'links': 'tbody tr a[href]', 'content': '.fulltext', 'title': 'h1',
        'next': '', 'guard': 'input[type="password"]:visible, #captcha:visible, .captcha:visible',
        'pages': '1', 'interval': '3', 'timeout': '20', 'retries': '2',
        'batch': '100', 'min_chars': '50'
    }
    labels = ['数据目录', '浏览器 CDP 地址', '列表页 URL 匹配（唯一）', '导入链接基准 URL',
              '链接 CSS', '正文 CSS', '标题 CSS', '下一页 CSS（空=仅本页）',
              '权限/验证码提示选择器', '最多采集页数', '请求间隔（秒）', '超时（秒）',
              '额外重试次数', '单批下载上限', '正文最少字符']
    variables = {}
    form = ttk.Frame(root, padding=12)
    form.pack(fill='x')
    for row, ((key, default), label) in enumerate(zip(defaults.items(), labels)):
        ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', pady=2)
        variables[key] = tk.StringVar(value=default)
        ttk.Entry(form, textvariable=variables[key]).grid(row=row, column=1, sticky='ew', padx=8)
    form.columnconfigure(1, weight=1)
    buttons = ttk.Frame(root, padding=8)
    buttons.pack(fill='x')
    status = tk.StringVar(value='就绪：请先启动调试浏览器并手动登录')
    ttk.Label(root, textvariable=status).pack(anchor='w', padx=12)
    log = tk.Text(root, height=14, wrap='word')
    log.pack(fill='both', expand=True, padx=12, pady=8)
    action_buttons = []

    def start(mode):
        nonlocal worker, control
        if worker and worker.is_alive():
            return
        settings = {k: v.get().strip() for k, v in variables.items()}
        try:
            for key in ('pages', 'timeout', 'batch', 'min_chars'):
                settings[key] = int(settings[key])
                if settings[key] <= 0:
                    raise ValueError('页数、超时、批量、字符数必须大于零')
            settings['retries'] = int(settings['retries'])
            settings['interval'] = float(settings['interval'])
            if not 0 <= settings['retries'] <= 5 or not 1 <= settings['interval'] <= 3600:
                raise ValueError('重试为 0–5 次，间隔为 1–3600 秒')
            endpoint = urlsplit(settings['endpoint'])
            if endpoint.scheme != 'http' or endpoint.hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('CDP 仅允许本机 HTTP 地址')
            if mode == 'import':
                settings['import_file'] = filedialog.askopenfilename(filetypes=[('URL 文本', '*.txt')])
                if not settings['import_file']:
                    return
        except ValueError as exc:
            messagebox.showerror('设置错误', str(exc))
            return
        control = Control()
        pause_button.configure(text='暂停')
        for button in action_buttons:
            button.configure(state='disabled')
        status.set('运行中')

        def work():
            try:
                Collector(settings, control, events.put).run(mode)
                events.put('本次操作完成，请查看任务统计中的失败数')
            except Cancelled:
                events.put('任务已停止，未完成任务保留在数据库中')
            except Exception as exc:
                events.put(f'任务中止：{type(exc).__name__}: {exc}')
            finally:
                events.put(None)
        worker = threading.Thread(target=work, daemon=True)
        worker.start()

    for label, mode in [('采集链接', 'collect'), ('下载正文', 'download'), ('导入 urls.txt', 'import'),
                        ('重新排队失败任务', 'retry'), ('导出统计', 'export')]:
        button = ttk.Button(buttons, text=label, command=lambda m=mode: start(m))
        button.pack(side='left', padx=3)
        action_buttons.append(button)

    def pause():
        if control.paused.is_set():
            control.paused.clear()
            pause_button.configure(text='暂停')
        else:
            control.paused.set()
            pause_button.configure(text='继续')
    pause_button = ttk.Button(buttons, text='暂停', command=pause)
    pause_button.pack(side='left', padx=3)
    ttk.Button(buttons, text='停止', command=lambda: control.stop.set()).pack(side='left')

    def poll():
        try:
            while True:
                item = events.get_nowait()
                if item is None:
                    for button in action_buttons:
                        button.configure(state='normal')
                    status.set('就绪')
                else:
                    log.insert('end', time.strftime('%H:%M:%S ') + item + '\n')
                    log.see('end')
        except queue.Empty:
            pass
        root.after(100, poll)

    def close():
        if worker and worker.is_alive():
            control.stop.set()
            status.set('正在停止，等待当前浏览器操作结束…')
            root.after(100, close)
        else:
            root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    poll()
    root.mainloop()


if __name__ == '__main__':
    launch_gui()
