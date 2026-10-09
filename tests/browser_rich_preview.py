"""真实浏览器：Word 图片、工作表样式、幻灯片图片和分库统计。"""
import os
import json
import socket
import tempfile
import threading
import time
from unittest.mock import patch
import httpx
import uvicorn
from playwright.sync_api import sync_playwright, expect
from app.main import create_app
from tests.test_service import FixtureEmbedding
from tests.test_rich_documents import office_fixture


def run():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'RAG_ADMIN_TOKEN':'browser-admin','RAG_MCP_TOKEN':''}):
        listener=socket.socket(); listener.bind(('127.0.0.1',0))
        server=uvicorn.Server(uvicorn.Config(create_app(directory,FixtureEmbedding()),log_level='critical',ws='none'))
        worker=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True); worker.start()
        try:
            for _ in range(250):
                if server.started: break
                time.sleep(.02)
            assert server.started
            base=f'http://127.0.0.1:{listener.getsockname()[1]}'
            with httpx.Client(base_url=base,headers={'Authorization':'Bearer browser-admin'},trust_env=False) as client:
                client.put('/api/config',json={'base_url':'https://example.org/v1','model':'fixture'})
                for suffix in ['.docx','.xlsx','.pptx']:
                    response=client.post('/api/documents',files={'file':('fixture'+suffix,office_fixture(suffix))})
                    assert response.status_code==201,response.text
            with sync_playwright() as p:
                browser=p.chromium.launch(); page=browser.new_page(viewport={'width':1440,'height':1000})
                errors=[]; violations=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('console',lambda msg:violations.append(msg.text) if msg.type=='error' and 'Content Security Policy' in msg.text else None)
                page.goto(base); page.locator('#admin-token').fill('browser-admin'); page.get_by_role('button',name='连接工作台').click()
                expect(page.locator('#document-count')).to_have_text('3'); expect(page.locator('#ready-count')).to_have_text('3'); expect(page.locator('#chunk-count')).to_have_text('3')
                pages=page.locator('[data-page]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.page)')
                assert pages.index('keys') < pages.index('service')
                page.locator('[data-library="faq"]').click()
                for id in ['document-count','ready-count','chunk-count']: expect(page.locator('#'+id)).to_have_text('0')
                expect(page.locator('#nav-count')).to_have_text('3')
                page.locator('[data-library="default"]').click()
                for suffix in ['.docx','.xlsx','.pptx']:
                    page.locator('.file-row').filter(has_text='fixture'+suffix).get_by_role('button',name='预览',exact=True).click()
                    if suffix=='.docx':
                        expect(page.locator('.word-preview-body img')).to_be_visible()
                        page.wait_for_function('document.querySelector(".word-preview-body img")?.naturalWidth > 0')
                        expect(page.locator('.word-preview-body')).to_contain_text('Native Word text')
                    elif suffix=='.xlsx':
                        expect(page.locator('.sheet-image')).to_be_visible()
                        assert page.locator('td[colspan="3"]').evaluate('(cell)=>getComputedStyle(cell).backgroundColor')=='rgb(255, 204, 0)'
                        page.locator('[data-sheet-tab="1"]').click(); expect(page.locator('[data-sheet-panel="1"]')).to_be_visible()
                        expect(page.locator('[data-sheet-panel="0"]')).to_be_hidden()
                        page.locator('[data-sheet-tab="0"]').click()
                    else:
                        expect(page.locator('.slide-page svg image')).to_be_visible(); expect(page.locator('.slide-page svg')).to_contain_text('Native slide text')
                    page.screenshot(path='artifacts/preview'+suffix+'.png',full_page=True)
                    page.locator('#preview-dialog').get_by_role('button',name='关闭',exact=True).click()
                for width in [1440,768,375,320]:
                    page.set_viewport_size({'width':width,'height':900})
                    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
                with httpx.Client(base_url=base,headers={'Authorization':'Bearer browser-admin'},trust_env=False) as client:
                    docs=client.get('/api/documents').json()['documents']
                    folder=client.post('/api/folders',json={'name':'统计子目录','library_id':'default'}).json()['id']
                    client.patch('/api/documents/'+docs[0]['id']+'/location',json={'library_id':'default','folder_id':folder})
                    client.patch('/api/documents/'+docs[0]['id'],json={'enabled':False})
                page.locator('#refresh-button').click()
                expect(page.locator('#document-count')).to_have_text('3'); expect(page.locator('#ready-count')).to_have_text('2'); expect(page.locator('#chunk-count')).to_have_text('2')
                page.locator('#folder-list').get_by_role('button',name='统计子目录').click()
                expect(page.locator('#document-count')).to_have_text('3')
                assert not errors,errors
                assert not violations, {'count':len(violations),'first':violations[:3]}
                browser.close()
                print(json.dumps({'结果':'通过','检查':['分库统计与平台计数分离','授权导航先于接入','Word图片与正文','Excel工作表切换、合并、底色、图片','PPT图片与原坐标','严格CSP无违规','响应式布局'],'页面错误':errors},ensure_ascii=False))
        finally:
            server.should_exit=True; worker.join(5); listener.close()


if __name__=='__main__': run()
