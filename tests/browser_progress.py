"""真实浏览器验证上传、重建中的持续进度与失败收尾。"""
import asyncio
import os
import socket
import tempfile
import threading
import time
from unittest.mock import patch
import httpx
import uvicorn
from playwright.sync_api import sync_playwright,expect
from app.main import create_app
from tests.test_service import FixtureEmbedding

class SlowEmbedding(FixtureEmbedding):
    async def embed(self,config,texts):
        await asyncio.sleep(2)
        return await super().embed(config,texts)

def run():
    with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{'RAG_ADMIN_TOKEN':'progress-admin','RAG_MCP_TOKEN':''}):
        embedding=SlowEmbedding();listener=socket.socket();listener.bind(('127.0.0.1',0))
        server=uvicorn.Server(uvicorn.Config(create_app(directory,embedding),log_level='critical',ws='none'))
        worker=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);worker.start()
        try:
            for _ in range(200):
                if server.started:break
                time.sleep(.02)
            base=f'http://127.0.0.1:{listener.getsockname()[1]}'
            with httpx.Client(base_url=base,headers={'Authorization':'Bearer progress-admin'},trust_env=False) as client:
                client.put('/api/config',json={'base_url':'https://example.org/v1','model':'fixture'})
            with sync_playwright() as p:
                browser=p.chromium.launch();page=browser.new_page(viewport={'width':1440,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base);page.locator('#admin-token').fill('progress-admin');page.get_by_role('button',name='连接工作台').click()
                expect(page.locator('#connection-state')).to_have_text('已连接 · 运行中')
                expect(page.locator('#file-input')).to_be_enabled()
                page.locator('#file-input').set_input_files([{'name':'one.txt','mimeType':'text/plain','buffer':b'first'},{'name':'two.txt','mimeType':'text/plain','buffer':b'second'}])
                expect(page.locator('#operation-progress')).to_be_visible()
                try:expect(page.locator('#task-stage-label')).to_contain_text('生成向量')
                except AssertionError:
                    print('合成上传错误详情：',page.locator('#upload-progress').inner_text(),errors)
                    raise
                expect(page.locator('#task-file')).to_contain_text('one.txt')
                assert page.locator('#task-overall').get_attribute('value')=='0'
                page.screenshot(path='data/合成资料进度条验收.png',full_page=True)
                expect(page.locator('#task-summary')).to_contain_text('已处理 2/2',timeout=15000)
                expect(page.locator('#upload-progress')).to_contain_text('2 份文件已保存')
                page.locator('#reindex-all').click()
                expect(page.locator('#task-title')).to_have_text('重建索引')
                expect(page.locator('#task-stage-label')).to_contain_text('生成向量')
                expect(page.locator('#task-summary')).to_contain_text('已处理 2/2',timeout=15000)
                expect(page.locator('#task-summary')).to_contain_text('完成')
                embedding.fail=True
                page.locator('.file-row').first.get_by_role('button',name='重建',exact=True).click()
                expect(page.locator('#task-summary')).to_contain_text('需检查',timeout=15000)
                expect(page.locator('#task-stage-label')).to_contain_text('失败')
                for width in [1440,768,375,320]:
                    page.set_viewport_size({'width':width,'height':900})
                    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
                assert not errors,errors
                browser.close();print('上传传输/处理阶段、批量文件进度、全部与单文件重建、失败收尾和响应式检查通过。')
        finally:server.should_exit=True;worker.join(5);listener.close()

if __name__=='__main__':run()
