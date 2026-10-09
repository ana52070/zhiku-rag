"""管理界面实际浏览器验收；模型调用使用测试替身，不访问付费接口。"""
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

from app.main import create_app


def run():
    class TestEmbedding:
        async def embed(self, config, texts):
            return [[1.0, 0.0] for text in texts]

    output = Path("artifacts")
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(directory, TestEmbedding()), log_level="critical", ws="none"))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            for _ in range(250):
                if server.started:
                    break
                time.sleep(0.02)
            assert server.started, "测试服务器未启动"
            with sync_playwright() as browser_tools:
                browser = browser_tools.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}")
                expect(page.locator("#connection-state")).to_have_text("已连接 · 运行中")
                page.screenshot(path=str(output / "desktop-empty.png"), full_page=True)
                page.get_by_role("button", name="模型配置", exact=True).click()
                page.locator("#base-url").fill("https://example.org/v1")
                page.locator("#model-name").fill("测试向量模型")
                page.locator("#api-key").fill("test-only-secret")
                page.get_by_role("button", name="测试连接", exact=True).click()
                expect(page.locator("#model-test-result")).to_contain_text("连接成功")
                page.get_by_role("button", name="保存配置", exact=True).click()
                expect(page.locator("#api-key")).to_have_value("")
                expect(page.locator("#key-state")).to_contain_text("已保存")
                page.screenshot(path=str(output / "desktop-model.png"), full_page=True)
                page.get_by_role("button", name="知识库", exact=False).first.click()
                page.locator("#file-input").set_input_files({"name": "雷达安装说明.md", "mimeType": "text/markdown", "buffer": "# 雷达安装\n\n安装前检查网口连接，完成后进行测距验证。".encode()})
                expect(page.locator("#file-list")).to_contain_text("可检索")
                expect(page.locator("#document-count")).to_have_text("1")
                page.screenshot(path=str(output / "desktop-library.png"), full_page=True)
                page.get_by_role("button", name="预览", exact=True).click()
                expect(page.locator("#preview-rendered")).to_contain_text("检查网口连接")
                page.locator("#preview-dialog").get_by_role("button", name="关闭", exact=True).click()
                page.get_by_role("button", name="检索试验", exact=True).click()
                page.locator("#search-query").fill("雷达安装前需要检查什么？")
                page.get_by_role("button", name="开始检索", exact=True).click()
                expect(page.locator("#search-results")).to_contain_text("检查网口连接")
                page.screenshot(path=str(output / "desktop-search.png"), full_page=True)
                page.get_by_role("button", name="服务与接入", exact=True).click()
                page.get_by_role("button", name="暂停业务", exact=True).click()
                expect(page.locator("#service-heading")).to_have_text("知识库业务已暂停")
                page.get_by_role("button", name="恢复业务", exact=True).click()
                expect(page.locator("#service-heading")).to_have_text("知识库业务正在运行")
                page.get_by_role("button", name="知识库", exact=False).first.click()
                page.get_by_role("button", name="禁用", exact=True).click()
                expect(page.locator("#file-list")).to_contain_text("已禁用")
                page.get_by_role("button", name="预览", exact=True).click()
                expect(page.locator("#preview-rendered")).to_contain_text("检查网口连接")
                page.locator("#preview-dialog").get_by_role("button", name="关闭", exact=True).click()
                page.get_by_role("button", name="启用", exact=True).click()
                expect(page.get_by_role("button", name="预览", exact=True)).to_be_enabled()
                widths = [1440, 1024, 768, 390, 320]
                for width in widths:
                    page.set_viewport_size({"width": width, "height": 900})
                    for name in ["知识库", "检索试验", "模型配置", "服务与接入"]:
                        page.get_by_role("button", name=name, exact=False).first.click()
                        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                        assert not overflow, f"{name} 在 {width}px 宽度发生横向溢出"
                    page.get_by_role("button", name="知识库", exact=False).first.click()
                    if width == 390:
                        page.screenshot(path=str(output / "mobile-library.png"), full_page=True)
                page.get_by_role("button", name="删除", exact=True).click()
                page.get_by_role("button", name="删除文件", exact=True).click()
                expect(page.locator("#document-count")).to_have_text("0")
                assert not errors, errors
                browser.close()
                print(json.dumps({"结果": "通过", "检查": ["模型连接测试与密钥清空", "上传与索引状态", "原文预览", "来源检索", "暂停恢复", "禁用启用", "删除", "五种宽度下四个页面无横向溢出"], "页面脚本错误": errors, "截图目录": str(output)}, ensure_ascii=False))
        finally:
            server.should_exit = True
            worker.join(5)
            listener.close()


if __name__ == "__main__":
    run()
