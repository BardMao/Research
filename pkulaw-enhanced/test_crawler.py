import tempfile
import unittest
from pathlib import Path
from crawler import Store, Control, Cancelled, Collector, AccessRequired, filename, normalize_url


class CoreTests(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(normalize_url('/a?q=1#x', 'https://example.com/list'), 'https://example.com/a?q=1')
        for value in ('javascript:alert(1)', '//evil.com/a', 'https://u:p@example.com/a'):
            with self.assertRaises(ValueError):
                normalize_url(value, 'https://example.com/')

    def test_filenames(self):
        a, b = filename('CON:/ same', 'https://a/1'), filename('CON:/ same', 'https://a/2')
        self.assertNotEqual(a, b)
        self.assertNotIn(':', a)
        self.assertTrue(a.startswith('doc_'))

    def test_resume(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            self.assertEqual(store.add(['https://a/1', 'https://a/1', 'https://a/2']), 2)
            store.record('https://a/1', 'done', title='=formula')
            store.record('https://a/2', 'failed', error='network')
            store.close()
            store = Store(folder)
            self.assertEqual(len(store.pending()), 0)
            store.retry_failed()
            self.assertEqual([r['url'] for r in store.pending()], ['https://a/2'])
            self.assertEqual(store.export(), {'done': 1, 'pending': 1})
            self.assertIn("'=formula", (Path(folder) / 'index.csv').read_text(encoding='utf-8-sig'))
            store.close()

    def test_stop_while_paused(self):
        control = Control()
        control.paused.set()
        control.stop.set()
        with self.assertRaises(Cancelled):
            control.wait(10)

    def test_access_stops_without_consuming_queue(self):
        class Page:
            url = 'https://a/login'
            def goto(self, *args, **kwargs):
                return None
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.add(['https://a/1'])
            collector = Collector({'batch': 10, 'retries': 2, 'interval': 0}, Control(), lambda x: None)
            with self.assertRaises(AccessRequired):
                collector.download(Page(), store)
            self.assertEqual(len(store.pending()), 1)
            store.close()

    def test_download_retries_and_saves(self):
        class Node:
            @property
            def first(self):
                return self
            def wait_for(self, **kwargs):
                pass
            def count(self):
                return 1
            def inner_text(self):
                return 'example document text'
        class Empty(Node):
            def count(self):
                return 0
        class Page:
            url = 'https://a/1'
            calls = 0
            def goto(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError('simulated timeout')
            def locator(self, selector):
                return Empty() if selector == '.guard' else Node()
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            store.add(['https://a/1'])
            collector = Collector({'batch': 10, 'retries': 1, 'interval': 0, 'guard': '.guard',
                                   'content': '.fulltext', 'title': 'h1', 'min_chars': 10},
                                  Control(), lambda x: None)
            collector.download(Page(), store)
            self.assertEqual(store.export(), {'done': 1})
            row = store.db.execute('SELECT * FROM tasks').fetchone()
            self.assertEqual(row['attempts'], 2)
            self.assertTrue((Path(folder) / row['file']).is_file())
            self.assertEqual(len(list(Path(folder).glob('doc_*.json'))), 1)
            store.close()


if __name__ == '__main__':
    unittest.main()
