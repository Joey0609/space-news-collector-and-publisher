# -*- coding: utf-8 -*-
"""离线测试本机事件向量、generation 提交及 USearch 恢复。"""
import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from collect.db import SCHEMA
from process import embedding
from process import analyze_db


class FakeModel:
    def encode(self, texts, **kwargs):
        vectors=[]
        for text in texts:
            vector=np.zeros(embedding.DIMENSIONS,dtype=np.float32)
            vector[0 if "火星" in text else 1]=1.0
            vectors.append(vector)
        return np.stack(vectors)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.db=self.root/'hx.sqlite3'
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(SCHEMA);conn.commit()
        self.conn=sqlite3.connect(self.db)
        self.conn.row_factory=sqlite3.Row
        self.index_file=self.root/'vectors/events.usearch'
        self.manifest=self.root/'state/event_vector_index.json'
        self.patches=[
            patch.object(embedding,'INDEX_FILE',self.index_file),
            patch.object(embedding,'MANIFEST_FILE',self.manifest),
            patch.object(embedding,'_index',None),
            patch.object(embedding,'_model',None),
        ]
        for item in self.patches:item.start()

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.conn.close();self.temp.cleanup()

    def add_item(self,item_id,text,title,stage='processed'):
        self.conn.execute("INSERT INTO items(id,raw,stage,ai_title,ai_summary,ts) VALUES(?,?,?,?,?,?)",
                          (item_id,text,stage,title,f"{title} 摘要",item_id))
        return dict(self.conn.execute('SELECT * FROM items WHERE id=?',(item_id,)).fetchone())

    def add_vector(self,item,position):
        vector=np.zeros(embedding.DIMENSIONS,dtype=np.float32);vector[position]=1
        context={'available':True,'input_hash':embedding.input_hash(item),'vector':vector.tolist()}
        embedding.save_embedding(self.conn,item,context)

    def test_scratch_database_cannot_write_production_sidecar(self):
        with patch.object(embedding,'INDEX_FILE',embedding._PRODUCTION_INDEX_FILE), \
             patch.object(embedding,'MANIFEST_FILE',embedding._PRODUCTION_MANIFEST_FILE):
            self.assertFalse(embedding._sidecar_allowed(self.conn))

    def test_hnsw_retrieval_filters_unprocessed_and_excluded_items(self):
        first=self.add_item(1,'火星任务进入轨道','火星探测器进入轨道')
        second=self.add_item(2,'木星任务进入轨道','木星探测器进入轨道')
        pending=self.add_item(3,'火星测试消息','待处理消息',stage='collected')
        self.add_vector(first,0);self.add_vector(second,1);self.add_vector(pending,0);self.conn.commit()
        self.assertEqual(embedding.retrieve(self.conn,np.eye(embedding.DIMENSIONS,dtype=np.float32)[0]),
                         ['#1 火星探测器进入轨道 | 火星探测器进入轨道 摘要'])
        self.assertEqual(embedding.retrieve(self.conn,np.eye(embedding.DIMENSIONS,dtype=np.float32)[0],exclude_item_id=1),[])
        self.assertTrue(self.index_file.is_file())

    def test_generation_commit_saves_vector_atomically_then_index_recovers(self):
        item=self.add_item(10,'火星返回器成功着陆','',stage='collected')
        self.conn.execute("INSERT INTO processing_events(item_id,generation,created_at) VALUES(10,7,'2026-01-01')")
        self.conn.commit()
        context={'available':True,'input_hash':embedding.input_hash(item),'vector':np.eye(embedding.DIMENSIONS,dtype=np.float32)[0].tolist()}
        analysis={'zh':'译文','quoted_zh':'','title':'着陆','summary':'火星返回器着陆。','importance':'中',
                  'score':70,'tags':['领域:航天'],'repeat_of':'','decision':'发','invalid_reason':''}
        self.assertTrue(analyze_db.save_ok(self.conn,item,analysis,1,7,context))
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM item_embeddings WHERE item_id=10').fetchone()[0],1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM processing_events WHERE item_id=10').fetchone()[0],0)
        self.assertTrue(embedding.sync_committed(self.conn,10))
        embedding._index=None
        self.assertEqual(len(embedding._get_index(self.conn)),1)

    def test_backfill_is_resumable_and_does_not_touch_analysis(self):
        self.add_item(20,'火星样本返回地球','已有标题')
        self.add_item(21,'木星新探测器抵达','另一标题')
        self.conn.commit()
        with patch.object(embedding,'_load_model',return_value=FakeModel()):
            result=embedding.backfill(self.conn,batch_size=1)
            second=embedding.backfill(self.conn,batch_size=1)
        self.assertEqual(result,{'pending':2,'embedded':2,'failed':0})
        self.assertEqual(second,{'pending':0,'embedded':0,'failed':0})
        self.assertEqual([r[0] for r in self.conn.execute('SELECT ai_title FROM items ORDER BY id')],['已有标题','另一标题'])
        self.assertEqual(len(embedding._get_index(self.conn)),2)


if __name__=='__main__':
    unittest.main()
