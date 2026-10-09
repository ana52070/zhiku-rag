"""单进程操作进度，只保存阶段与计数，不写入业务数据库。"""
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

_current=ContextVar('operation_progress',default=None)


def report(**values):
    current=_current.get()
    if current:
        registry,identifier=current
        registry.update(identifier,**values)


def document_done(failed=False):
    current=_current.get()
    if current:
        registry,identifier=current
        with registry.lock:
            item=registry.items[identifier]
            registry.update(identifier,completed=item['completed']+1,failed=item['failed']+int(failed))


class Operations:
    def __init__(self):
        self.items={};self.lock=threading.RLock()

    def update(self,identifier,**values):
        with self.lock:self.items[identifier].update(values,updated=time.time())

    def get(self,identifier):
        from .service import BusinessError
        with self.lock:
            if identifier not in self.items:raise BusinessError('操作进度不存在或已过期。',404)
            return dict(self.items[identifier])

    @contextmanager
    def track(self,identifier,kind):
        from .service import BusinessError
        if not identifier:
            yield
            return
        with self.lock:
            now=time.time()
            for key,item in list(self.items.items()):
                if item['state']!='running' and now-item['updated']>3600:del self.items[key]
            if identifier in self.items:raise BusinessError('操作标识已使用，请重新开始。',409)
            if sum(item['state']=='running' for item in self.items.values())>=8:raise BusinessError('正在处理的操作过多，请稍后重试。',429)
            while len(self.items)>=200:
                del self.items[next(key for key,item in self.items.items() if item['state']!='running')]
            self.items[identifier]={'id':identifier,'kind':kind,'state':'running','stage':'queued','filename':'','total':0,'completed':0,'failed':0,'current':0,'stage_total':None,'error':'','updated':now}
        token=_current.set((self,identifier))
        try:yield
        except Exception as error:
            message=error.message if isinstance(error,BusinessError) else str(error) if isinstance(error,ValueError) else '处理被中断，请检查文件状态。'
            self.update(identifier,state='failed',error=message)
            raise
        else:
            item=self.get(identifier)
            self.update(identifier,state='failed' if item['failed'] else 'complete',stage='done',current=0,stage_total=None)
        finally:_current.reset(token)
