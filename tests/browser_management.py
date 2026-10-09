"""通过真实浏览器验证多选、预览、知识库与 Key 生命周期。"""
import json
import socket
import tempfile
import threading
import time
from unittest.mock import patch
import os
import uvicorn
from playwright.sync_api import sync_playwright, expect
from app.main import create_app
from tests.test_service import FixtureEmbedding


def run():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RAG_ADMIN_TOKEN': 'browser-admin', 'RAG_MCP_TOKEN': ''}):
        listener = socket.socket(); listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(create_app(directory, FixtureEmbedding()), log_level='critical', ws='none'))
        worker = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True); worker.start()
        try:
            for _ in range(250):
                if server.started: break
                time.sleep(.02)
            assert server.started
            with sync_playwright() as p:
                browser = p.chromium.launch(); page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{listener.getsockname()[1]}')
                page.locator('#admin-token').fill('browser-admin'); page.get_by_role('button', name='连接工作台').click()
                expect(page.locator('#connection-state')).to_have_text('已连接 · 运行中')
                page.locator('#rename-library').click(); page.locator('#resource-name').fill('整理资料')
                page.locator('#resource-form').get_by_role('button', name='保存').click()
                expect(page.locator('#collection-title')).to_have_text('整理资料')
                from tests.document_fixtures import text_pdf
                page.locator('#file-input').set_input_files({'name': 'manual.pdf', 'mimeType': 'application/pdf', 'buffer': text_pdf()})
                expect(page.locator('.file-row')).to_have_count(1)
                page.get_by_role('button', name='预览', exact=True).click()
                expect(page.locator('#preview-pdf')).to_be_visible()
                assert page.locator('#preview-pdf').get_attribute('src').startswith('blob:')
                page.locator('#preview-mode').click()
                expect(page.locator('#preview-content')).to_contain_text('PDF preview test')
                page.locator('#preview-dialog').get_by_role('button', name='关闭', exact=True).click()
                page.locator('#select-all').check(); page.locator('#batch-delete').click(); page.locator('#confirm-management').click()
                expect(page.locator('.file-row')).to_have_count(0)
                page.locator('#file-input').set_input_files([{'name': 'a.md', 'mimeType': 'text/markdown', 'buffer': b'# Preview heading'}, {'name': 'b.txt', 'mimeType': 'text/plain', 'buffer': b'body text'}])
                expect(page.locator('.file-row')).to_have_count(2)
                page.locator('.file-row').filter(has_text='a.md').get_by_role('button', name='预览', exact=True).click()
                expect(page.locator('#preview-rendered h1')).to_have_text('Preview heading')
                page.locator('#preview-dialog').get_by_role('button', name='关闭', exact=True).click()
                page.locator('#select-all').check(); expect(page.locator('#selection-count')).to_have_text('已选 2 份')
                page.locator('#batch-move').click(); page.locator('#move-library').select_option('faq')
                page.locator('#move-dialog').get_by_role('button', name='移动', exact=True).click()
                expect(page.locator('.file-row')).to_have_count(0)
                page.locator('[data-library="faq"]').click(); expect(page.locator('.file-row')).to_have_count(2)
                page.locator('#new-folder').click(); page.locator('#resource-name').fill('目标文件夹')
                page.locator('#resource-form').get_by_role('button', name='创建', exact=True).click()
                page.locator('#select-all').check(); page.locator('#batch-move').click()
                page.locator('#move-folder').select_option(label='目标文件夹')
                page.locator('#move-dialog').get_by_role('button', name='移动', exact=True).click()
                expect(page.locator('.file-row')).to_have_count(0)
                page.locator('#folder-list').get_by_role('button', name='目标文件夹').click()
                expect(page.locator('.file-row')).to_have_count(2)
                page.locator('#select-all').check(); page.locator('#batch-delete').click(); page.locator('#confirm-management').click()
                expect(page.locator('.file-row')).to_have_count(0)
                page.locator('#delete-library').click(); page.locator('#confirm-management').click()
                expect(page.locator('[data-library="faq"]')).to_have_count(0)
                page.locator('[data-page="keys"]').click(); page.locator('#new-key').click()
                page.locator('#key-name').fill('生命周期'); page.locator('#key-form').get_by_role('button', name='保存授权').click()
                expect(page.locator('#secret-dialog')).to_be_visible()
                page.locator('#secret-dialog').get_by_role('button', name='已保存，关闭').click()
                page.locator('[data-key-revoke]').click(); page.locator('#confirm-revoke').click()
                expect(page.locator('#key-list')).to_contain_text('已撤销')
                page.locator('[data-key-delete]').click(); page.locator('#confirm-management').click()
                expect(page.locator('#key-list')).not_to_contain_text('生命周期')
                page.locator('[data-page="service"]').click(); page.locator('#mcp-key').fill('zk_fixture_only')
                config = json.loads(page.locator('#mcp-json').input_value())
                assert config['mcp']['servers']['zhiku-rag']['headers']['Authorization'] == 'Bearer zk_fixture_only'
                assert 'zk_fixture_only' in page.locator('#agent-prompt').input_value()
                page.locator('#mcp-format').select_option('generic')
                assert 'mcpServers' in json.loads(page.locator('#mcp-json').input_value())
                for width in [1440, 768, 375, 320]:
                    page.set_viewport_size({'width': width, 'height': 900})
                    for name in ['library', 'service', 'keys']:
                        page.locator(f'[data-page="{name}"]').click()
                        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), (width, name)
                page.set_viewport_size({'width': 1440, 'height': 1000}); page.locator('[data-page="library"]').click()
                page.screenshot(path='artifacts/management-library.png', full_page=True)
                page.locator('[data-page="service"]').click(); page.locator('#mcp-key').fill('')
                page.screenshot(path='artifacts/management-mcp.png', full_page=True)
                assert not errors, errors
                browser.close()
                print(json.dumps({'结果': '通过', '验证': ['知识库改名和删除', '多选批量跨库及同库文件夹移动', '批量删除', '文档预览', '已撤销Key永久删除', 'OpenClaw和通用JSON', '自动配置提示词', '响应式布局'], '页面错误': errors}, ensure_ascii=False))
        finally:
            server.should_exit = True; worker.join(5); listener.close()


if __name__ == '__main__':
    run()
