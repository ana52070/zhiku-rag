"""知识库与逻辑目录；不使用用户输入拼接服务器文件路径。"""
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field, field_validator
from .service import BusinessError


class NamedResource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)

    @field_validator('name')
    @classmethod
    def safe_name(cls, value):
        value = value.strip()
        if not value or value in {'.', '..'} or any(c in value for c in '/\\\x00'):
            raise ValueError('名称不能为空，也不能包含路径分隔符。')
        return value


class Location(BaseModel):
    model_config = ConfigDict(extra='forbid')
    library_id: str = 'default'
    folder_id: str | None = None


class FolderRequest(NamedResource):
    library_id: str
    parent_id: str | None = None


class Catalog:
    def __init__(self, storage):
        self.storage = storage

    def libraries(self):
        with self.storage.connect() as db:
            return [dict(row) for row in db.execute("SELECT l.*,COUNT(d.id) AS document_count FROM libraries l LEFT JOIN documents d ON d.library_id=l.id GROUP BY l.id ORDER BY CASE l.id WHEN 'default' THEN 0 WHEN 'faq' THEN 1 WHEN 'retrospective' THEN 2 WHEN 'projects' THEN 3 ELSE 4 END,l.created_at")]

    def folders(self, library_id=None):
        with self.storage.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM folders WHERE (? IS NULL OR library_id=?) ORDER BY name', (library_id, library_id))]

    def validate_location(self, library_id, folder_id=None):
        with self.storage.connect() as db:
            if not db.execute('SELECT 1 FROM libraries WHERE id=?', (library_id,)).fetchone():
                raise BusinessError('知识库不存在。', 404)
            if folder_id and not db.execute('SELECT 1 FROM folders WHERE id=? AND library_id=?', (folder_id, library_id)).fetchone():
                raise BusinessError('文件夹不存在或属于其他知识库。')

    def create_library(self, name):
        resource = {'id': uuid.uuid4().hex, 'name': name, 'created_at': datetime.now(timezone.utc).isoformat()}
        with self.storage.connect() as db:
            if db.execute('SELECT 1 FROM libraries WHERE name=?', (name,)).fetchone():
                raise BusinessError('知识库名称已存在。', 409)
            db.execute('INSERT INTO libraries VALUES (?,?,?)', tuple(resource.values()))
        return resource

    def create_folder(self, request):
        self.validate_location(request.library_id, request.parent_id)
        resource = {'id': uuid.uuid4().hex, 'library_id': request.library_id, 'parent_id': request.parent_id, 'name': request.name}
        with self.storage.connect() as db:
            if db.execute('SELECT 1 FROM folders WHERE library_id=? AND parent_id IS ? AND name=?', (request.library_id, request.parent_id, request.name)).fetchone():
                raise BusinessError('同级文件夹名称已存在。', 409)
            db.execute('INSERT INTO folders VALUES (?,?,?,?)', tuple(resource.values()))
        return resource

    def delete_folder(self, folder_id):
        with self.storage.connect() as db:
            if not db.execute('SELECT 1 FROM folders WHERE id=?', (folder_id,)).fetchone():
                raise BusinessError('文件夹不存在。', 404)
            if db.execute('SELECT 1 FROM folders WHERE parent_id=?', (folder_id,)).fetchone() or db.execute('SELECT 1 FROM documents WHERE folder_id=?', (folder_id,)).fetchone():
                raise BusinessError('请先移走文件和子文件夹，再删除空文件夹。', 409)
            db.execute('DELETE FROM folders WHERE id=?', (folder_id,))
        return {'deleted': True}

    def delete_library(self, library_id):
        self.validate_location(library_id)
        with self.storage.connect() as db:
            if library_id == 'default' or db.execute('SELECT 1 FROM documents WHERE library_id=?', (library_id,)).fetchone() or db.execute('SELECT 1 FROM folders WHERE library_id=?', (library_id,)).fetchone():
                raise BusinessError('默认知识库或非空知识库不能删除。', 409)
            db.execute('DELETE FROM libraries WHERE id=?', (library_id,))
        return {'deleted': True}
