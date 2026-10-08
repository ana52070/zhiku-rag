"""用管理员身份验收目录、授权、带令牌下载和响应式布局。"""
import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
import uvicorn
from playwright.sync_api import expect, sync_playwright
from app.main import create_app
from tests.test_service import FixtureEmbedding


def run():
    output = Path('artifacts')
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RAG_ADMIN_TOKEN': 'browser-admin', 'RAG_MCP_TOKEN': ''}):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(create_app(directory, FixtureEmbedding()), log_level='critical', ws='none'))
        worker = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
        worker.start()
        try:
            for _ in range(250):
                if server.started:
                    break
                time.sleep(.02)
            assert server.started
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{listener.getsockname()[1]}')
                page.locator('#admin-token').fill('browser-admin')
                page.get_by_role('button', name='连接工作台').click()
                expect(page.locator('#connection-state')).to_have_text('已连接 · 运行中')
                expect(page.locator('.brand img')).to_be_visible()
                assert page.locator('.brand img').evaluate('(img) => img.naturalWidth > 0')
                page.locator('[data-library="faq"]').click()
                page.locator('#new-folder').click()
                page.locator('#resource-name').fill('产品调试')
                page.locator('#resource-dialog').get_by_role('button', name='创建', exact=True).click()
                page.locator('#folder-list').get_by_role('button', name='产品调试').click()
                page.locator('#new-folder').click()
                page.locator('#resource-name').fill('网络配置')
                page.locator('#resource-dialog').get_by_role('button', name='创建', exact=True).click()
                page.locator('#folder-list').get_by_role('button', name='网络配置').click()
                page.locator('#file-input').set_input_files({'name': '目录说明.md', 'mimeType': 'text/markdown', 'buffer': '雷达网络配置说明'.encode()})
                expect(page.locator('#file-list')).to_contain_text('目录说明.md')
                expect(page.locator('#folder-breadcrumb')).to_contain_text('产品调试')
                with page.expect_download() as event:
                    page.get_by_role('link', name='下载', exact=True).click()
                download = event.value
                assert download.suggested_filename == '目录说明.md'
                assert Path(download.path()).read_bytes() == '雷达网络配置说明'.encode()
                page.get_by_role('button', name='移动', exact=True).click()
                page.locator('#move-library').select_option('retrospective')
                page.locator('#move-dialog').get_by_role('button', name='移动', exact=True).click()
                expect(page.locator('#file-list')).not_to_contain_text('目录说明.md')
                page.locator('[data-library="retrospective"]').click()
                expect(page.locator('#file-list')).to_contain_text('目录说明.md')
                page.screenshot(path=str(output / 'desktop-catalog.png'), full_page=True)
                page.locator('[data-page="keys"]').click()
                page.locator('#new-key').click()
                page.locator('#key-name').fill('OpenClaw 全库查询')
                page.locator('#key-dialog').get_by_role('button', name='保存授权').click()
                expect(page.locator('#created-key')).to_have_value(__import__('re').compile('zk_.+'))
                page.locator('#secret-dialog').get_by_role('button', name='已保存，关闭').click()
                expect(page.locator('#created-key')).to_have_value('')
                expect(page.locator('#key-list')).to_contain_text('全部知识库')
                page.locator('[data-key-edit]').click()
                page.locator('#key-all').uncheck()
                page.locator('#key-scopes input[value="faq"]').check()
                page.locator('#key-dialog').get_by_role('button', name='保存授权').click()
                expect(page.locator('#key-list')).to_contain_text('技术组FAQ查询')
                page.screenshot(path=str(output / 'desktop-keys.png'), full_page=True)
                page.locator('[data-key-revoke]').click()
                page.locator('#confirm-revoke').click()
                expect(page.locator('#key-list')).to_contain_text('已撤销')
                for width in [1440, 1024, 768, 375, 320]:
                    page.set_viewport_size({'width': width, 'height': 900})
                    for name in ['library', 'search', 'models', 'service', 'keys']:
                        page.locator(f'[data-page="{name}"]').click()
                        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'), (width, name)
                page.set_viewport_size({'width': 375, 'height': 812})
                page.locator('[data-page="library"]').click()
                page.emulate_media(reduced_motion='reduce')
                page.screenshot(path=str(output / 'mobile-catalog.png'), full_page=True)
                assert not errors, errors
                browser.close()
                print(json.dumps({'结果': '通过', '检查': ['公司原图加载', '管理员鉴权', '两层子文件夹', '定向上传', '带令牌下载原文件', '跨库移动', 'Key创建、范围修改和撤销', '明文关闭清除', '五种宽度五个页面无溢出'], '页面错误': errors}, ensure_ascii=False))
        finally:
            server.should_exit = True
            worker.join(5)
            listener.close()


if __name__ == '__main__':
    run()
