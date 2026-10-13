"""只读 SMB 客户端。扫描错误必须向上传播，禁止返回不完整清单。"""
import hashlib
import ntpath
import stat
from contextlib import contextmanager, closing

MAX_BYTES = 50 * 1024 * 1024
SUFFIXES = {'.txt', '.md', '.pdf', '.docx', '.xlsx', '.pptx', '.csv'}


def safe_parts(path, allow_empty=False):
    if not path and allow_empty:
        return []
    if not isinstance(path, str) or not path or '\\' in path or ':' in path or '\x00' in path:
        raise ValueError('目录必须是相对路径，不可包含盘符或特殊字符。')
    parts = path.split('/')
    if any(p in ('', '.', '..') for p in parts):
        raise ValueError('目录不可包含空层级、. 或 ..。')
    return parts


def validate_connection(cfg):
    server, share = cfg.get('server', ''), cfg.get('share', '')
    if not server or len(server) > 253 or any(c in server for c in '/\\:@\x00 \n\r'):
        raise ValueError('请填写目标 SMB 主机名或 IPv4 地址。')
    safe_parts(share)
    if '/' in share:
        raise ValueError('共享名只能是一个层级。')
    subdir = cfg.get('subdir', '').replace('\\', '/').strip('/')
    parts = safe_parts(subdir, allow_empty=True)
    port = cfg.get('port', 445)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('SMB 端口无效。')
    return ntpath.join('\\\\' + server, share, *parts)


class Source:
    def __init__(self, root, fs, kwargs=None):
        self.root, self.fs, self.kwargs = root, fs, kwargs or {}
        self.join = ntpath.join if root.startswith('\\\\') else __import__('os').path.join

    def _path(self, relative):
        parts = safe_parts(relative)
        path = self.root
        for part in parts:
            path = self.join(path, part)
            info = self.fs.stat(path, follow_symlinks=False, **self.kwargs)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('不允许读取符号链接或重解析点。')
        return path

    def read(self, relative, expected_hash=None, max_bytes=MAX_BYTES):
        path = self._path(relative)
        before = self.fs.stat(path, follow_symlinks=False, **self.kwargs)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError('文件类型无效或超过 50 MiB 限制。')
        if hasattr(self.fs, 'open_file'):
            handle = self.fs.open_file(path, mode='rb', share_access='r', **self.kwargs)
        else:
            handle = open(path, 'rb')
        with handle as file:
            content = file.read(max_bytes + 1)
        after = self.fs.stat(path, follow_symlinks=False, **self.kwargs)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(content) != before.st_size:
            raise ValueError('读取期间源文件发生变化，请重试。')
        if len(content) > max_bytes:
            raise ValueError('文件超过容量限制。')
        if expected_hash and hashlib.sha256(content).hexdigest() != expected_hash:
            raise ValueError('扫描后源文件已变化，取消本次读取。')
        return content

    def scan(self):
        root_info = self.fs.stat(self.root, follow_symlinks=False, **self.kwargs)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode) or getattr(root_info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('扫描根目录无效。')
        files = []
        pending = [('', 0)]
        visited = 0
        while pending:
            relative, depth = pending.pop()
            if depth > 64:
                raise ValueError('目录层级超过 64 层，扫描取消。')
            path = self.join(self.root, *relative.split('/')) if relative else self.root
            with closing(self.fs.scandir(path, **self.kwargs)) as iterator:
                entries = sorted(iterator, key=lambda e: e.name)
            for entry in entries:
                visited += 1
                if visited > 100000:
                    raise ValueError('目录条目超过 100000，扫描取消。')
                safe_parts(entry.name)
                name = relative + '/' + entry.name if relative else entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append((name, depth + 1))
                elif stat.S_ISREG(info.st_mode) and not entry.name.startswith('~$') and ntpath.splitext(entry.name)[1].lower() in SUFFIXES:
                    content = self.read(name)
                    files.append({'path': name, 'size': len(content), 'hash': hashlib.sha256(content).hexdigest()})
        # 根目录必须仍然可访问；失败不允许交付完整标记。
        self.fs.stat(self.root, **self.kwargs)
        return {'complete': True, 'files': sorted(files, key=lambda f: f['path'])}


@contextmanager
def connect(cfg):
    import smbclient
    root = validate_connection(cfg)
    cache = {}
    kwargs = {'username': cfg.get('username', ''), 'password': cfg.get('password', ''),
              'port': cfg.get('port', 445), 'connection_timeout': 30, 'connection_cache': cache}
    try:
        yield Source(root, smbclient, kwargs)
    finally:
        smbclient.reset_connection_cache(fail_on_error=False, connection_cache=cache)
