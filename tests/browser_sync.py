"""在真实同步测试服务中检查桌面/手机任务配置页面。"""
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
from tests.test_sync_e2e import SyncE2E


class BrowserSync(unittest.TestCase):
    def test_task_form_and_mobile(self):
        SyncE2E.setUpClass()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(error.stack))
                page.goto(SyncE2E.url + '/#sync')
                self.assertTrue(page.locator('#page-sync').is_visible(), '目录同步应支持直接链接')
                page.locator('#auth-dialog').wait_for(state='visible')
                page.locator('#admin-token').fill('admin-test')
                page.locator('#auth-form button[type="submit"]').click()
                page.locator('#new-sync').click()
                page.locator('#sync-name').fill('网页自动化测试')
                page.locator('#sync-server').fill('test-server')
                page.locator('#sync-share').fill('资料')
                page.locator('#sync-password').fill('web-secret')
                page.locator('#sync-test').click()
                page.locator('#sync-feedback').filter(has_text='连接成功').wait_for()
                page.locator('#sync-kind').select_option('cron')
                page.locator('#sync-cron').fill('0 3 * * 1-5')
                page.locator('#sync-preview').click()
                page.locator('#sync-feedback').filter(has_text='下次执行').wait_for()
                page.locator('#sync-enabled').uncheck()
                page.locator('#sync-form button[type="submit"]').click()
                page.locator('#sync-list').filter(has_text='网页自动化测试').wait_for()
                page.locator('[data-sync-action="edit"]').first.click()
                self.assertEqual(page.locator('#sync-password').input_value(), '')
                page.locator('#sync-cancel').click()
                # 本地来源必须可保存且不要求 SMB 主机/凭据。
                local_library = SyncE2E.client.post('/api/libraries', json={'name': '网页本地手册库'}).json()
                page.locator('#new-sync').click()
                page.locator('#sync-source-kind').select_option('local')
                page.locator('#sync-name').fill('本地手册同步')
                page.locator('#sync-source-id').fill('manual')
                page.locator('#sync-library').select_option(local_library['id'])
                self.assertFalse(page.locator('#sync-server').is_visible())
                page.locator('#sync-test').click()
                page.locator('#sync-feedback').filter(has_text='连接成功').wait_for()
                page.locator('#sync-enabled').uncheck()
                page.locator('#sync-form button[type="submit"]').click()
                page.locator('#sync-list').filter(has_text='本地手册同步').wait_for()
                local_card = page.locator('#sync-list .sync-card').filter(has_text='本地手册同步')
                self.assertIn('manual', local_card.inner_text())
                local_card.locator('[data-sync-action="edit"]').click()
                self.assertEqual(page.locator('#sync-source-kind').input_value(), 'local')
                self.assertEqual(page.locator('#sync-source-id').input_value(), 'manual')
                page.locator('#sync-cancel').click()
                page.locator('[data-sync-action="runs"]').first.click()
                page.locator('#sync-runs').filter(has_text='暂无执行记录').wait_for()
                Path('artifacts').mkdir(exist_ok=True)
                page.screenshot(path='artifacts/handbook-sync-desktop.png', full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#new-sync').click()
                page.locator('#sync-source-kind').select_option('local')
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                page.screenshot(path='artifacts/handbook-sync-mobile.png', full_page=True)
                self.assertEqual(errors, [])
                browser.close()
        finally:
            SyncE2E.tearDownClass()


if __name__ == '__main__':
    unittest.main()
