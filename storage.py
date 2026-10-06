"""Persistent order state and seed initialization for one app instance."""
from dataclasses import asdict
from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sqlite3
from threading import RLock
from converter import Line, Order, decstr

DECIMAL_FIELDS = ('quantity','unit_price','amount','feet','inches','multiplier')
SOURCE_FIELDS = ('n','page','code','description','quantity','uom','unit_price','amount','kind')

def decode_order(payload):
    value = json.loads(payload) if isinstance(payload, str) else payload.copy()
    value['lines'] = [Line(**{k: Decimal(v) if k in DECIMAL_FIELDS else v for k,v in row.items()}) for row in value['lines']]
    for key in ('untaxed','tax','total'):
        if value[key] is not None: value[key] = Decimal(value[key])
    return Order(**value)

def encode_order(order):
    return json.dumps(asdict(order), default=decstr, ensure_ascii=False)

def remap_order(order, mapper):
    order.lines = [Line(**{key:getattr(line,key) for key in SOURCE_FIELDS}) for line in order.lines]
    mapper.map_order(order)
    return order

class Store:
    def __init__(self, path, seed):
        self.root = Path(path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.uploads = self.root/'uploads'; self.uploads.mkdir(exist_ok=True)
        self.lock = RLock()
        for name in ('catalog.json','project_mappings.json','sources.json'):
            if not (self.root/name).exists():
                source=Path(seed)/name
                if source.exists(): shutil.copyfile(source,self.root/name)
                else: (self.root/name).write_text('{}',encoding='utf-8')
        self.db = self.root/'orders.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, order_id TEXT NOT NULL, payload TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def listing(self):
        with self.connect() as db: rows = db.execute('SELECT * FROM orders ORDER BY updated_at DESC, id').fetchall()
        return [dict(row) for row in rows]

    def get(self, key):
        with self.connect() as db: row = db.execute('SELECT * FROM orders WHERE id=?', (key,)).fetchone()
        if not row: return None
        order = decode_order(row['payload'])
        order.source_pdf = str(self.uploads/(key+'.pdf'))
        return order, row['revision']

    def insert(self, order):
        with self.connect() as db:
            result = db.execute('INSERT OR IGNORE INTO orders (id,order_id,payload) VALUES (?,?,?)', (order.sha256, order.order_id, encode_order(order)))
            return bool(result.rowcount)

    def update(self, order, revision):
        with self.connect() as db:
            result = db.execute('UPDATE orders SET payload=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND revision=?', (encode_order(order), order.sha256, revision))
            if not result.rowcount: raise ValueError('This order was changed in another tab. Reload and review the current version.')

    def delete(self, key):
        with self.connect() as db: db.execute('DELETE FROM orders WHERE id=?',(key,))
        (self.uploads/(key+'.pdf')).unlink(missing_ok=True)
