from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xmlrpc.client

from fixtures import write_seed
from odoo_api import fetch_order, order_from_records, OdooError
from converter import Mapper, p10_rows, review_rows, validate_export
from storage import encode_order, decode_order, remap_order
from webapp import create_app

CONFIG = dict(ODOO_URL='https://example.odoo.com', ODOO_DB='example-db',
              ODOO_USERNAME='example@example.com', ODOO_API_KEY='private-test-key')

def records():
    header = dict(id=17,name='S123',partner_id=[2,'Example Customer'],date_order='2026-10-07 12:00:00',
                  user_id=[3,'Example User'],amount_untaxed=90,amount_tax=9,amount_total=99,
                  order_line=[10,11,12,13],state='sale',currency_id=[1,'USD'],write_date='2026-10-07 12:00:00')
    line = dict(id=10,sequence=1,name='[EXAMPLE] Panel (12\', 0")', product_id=[42,'Panel'],product_uom_qty=10,
                product_uom=[1,'Pcs'],price_unit=11,discount=10,price_subtotal=99,qty_delivered=4,
                display_type=False,is_downpayment=False,write_date=header['write_date'])
    note = dict(line,id=11,sequence=2,name='Keep this note',product_id=False,product_uom_qty=0,
                price_unit=0,discount=0,price_subtotal=0,qty_delivered=0,display_type='line_note')
    discount = dict(line,id=12,sequence=3,name='Discount',product_uom_qty=1,price_unit=-9,
                    discount=0,price_subtotal=-9,qty_delivered=0)
    deposit = dict(line,id=13,sequence=4,name='Down payment',product_uom_qty=0,price_unit=50,
                   price_subtotal=0,qty_delivered=0,is_downpayment=True)
    return header,[line,note,discount,deposit],{42:dict(id=42,default_code='EXAMPLE')}

def snapshot():
    return order_from_records(*records(),CONFIG['ODOO_URL'],CONFIG['ODOO_DB'])

class OdooTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.seed=write_seed(Path(self.tmp.name)/'seed')
        self.config=dict(CONFIG,TESTING=True,DATA_DIR=str(Path(self.tmp.name)/'data'),
                         SEED_DIR=str(self.seed),APP_PASSWORD='test-login-password')
        self.app=create_app(self.config);self.client=self.app.test_client()
        self.client.get('/login')
        self.client.post('/login',data={'csrf_token':self.token(),'password':self.config['APP_PASSWORD']})

    def token(self):
        with self.client.session_transaction() as s:return s['csrf']

    def test_full_quantity_discount_delivery_and_p10_export(self):
        order=snapshot();mapper=Mapper(self.seed)
        self.assertEqual(order.lines[0].quantity,D(10))
        self.assertEqual(order.lines[0].delivered_quantity,D(4))
        self.assertEqual(order.lines[0].unit_price,D('9.9'))
        self.assertEqual(order.source_sum,D(90))
        self.assertTrue(order.has_deliveries)
        mapper.map_order(order)
        for line in order.lines:
            if line.kind in ('item','note'):mapper.approve_mi_com(line)
        validate_export(order)
        rows=p10_rows(order)
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[0][3],D(10))
        self.assertIn('DELIVERED: 4 Pcs',rows[0][4])
        self.assertEqual(rows[0][8],order.lines[0].description)
        self.assertEqual(rows[2][5],D(-9))
        self.assertEqual(rows[1][0],'COM')
        self.assertIn(D(4),review_rows(order)[0])

    def test_persistence_remap_and_old_payload_compatibility(self):
        order=decode_order(encode_order(snapshot()))
        remap_order(order,Mapper(self.seed))
        self.assertEqual(order.lines[0].delivered_quantity,D(4))
        self.assertTrue(order.has_deliveries)
        self.assertEqual(order.source_type,'odoo')
        import json
        value=json.loads(encode_order(order))
        for key in ('source_type','imported_at','odoo_record_id','odoo_state'):value.pop(key)
        for line in value['lines']:line.pop('delivered_quantity')
        old=decode_order(value)
        self.assertEqual(old.source_type,'pdf');self.assertFalse(old.has_deliveries)

    def test_snapshots_deduplicate_but_delivery_change_creates_new_snapshot(self):
        self.assertEqual(snapshot().sha256,snapshot().sha256)
        h,rows,p=records();rows[0]['qty_delivered']=5
        self.assertNotEqual(snapshot().sha256,order_from_records(h,rows,p,CONFIG['ODOO_URL'],CONFIG['ODOO_DB']).sha256)

    def test_import_routes_restart_audit_and_secret_exclusion(self):
        with patch('webapp.fetch_order',side_effect=lambda *args:snapshot()):
            response=self.client.post('/odoo/import',data={'csrf_token':self.token(),'order_number':'S123'})
            self.assertEqual(response.status_code,302)
            key=response.location.rsplit('/',1)[-1]
            self.client.post('/odoo/import',data={'csrf_token':self.token(),'order_number':'S123'})
        self.assertEqual(len(self.app.extensions['order_store'].listing()),1)
        for path in ['/',response.location,f'/orders/{key}/review/html',f'/orders/{key}/review/csv']:
            result=self.client.get(path)
            self.assertEqual(result.status_code,200)
            self.assertIn(b'Delivered quantities present',result.data)
            self.assertNotIn(CONFIG['ODOO_API_KEY'].encode(),result.data)
        self.assertEqual(self.client.get(f'/orders/{key}/source').status_code,404)
        saved=create_app(self.config).extensions['order_store'].get(key)[0]
        self.assertTrue(saved.has_deliveries)
        self.assertEqual(saved.source_pdf,'')
        self.assertNotIn(CONFIG['ODOO_API_KEY'],encode_order(saved))
        self.assertEqual(self.client.post('/odoo/import',data={}).status_code,400)
        anonymous=self.app.test_client();anonymous.get('/login')
        with anonymous.session_transaction() as s: token=s['csrf']
        self.assertEqual(anonymous.post('/odoo/import',data={'csrf_token':token}).status_code,302)

    def test_connection_failure_returns_friendly_page(self):
        with patch('webapp.fetch_order',side_effect=OdooError('Odoo sign-in failed.')):
            response=self.client.post('/odoo/import',data={'csrf_token':self.token(),'order_number':'S123'},follow_redirects=True)
        self.assertEqual(response.status_code,200);self.assertIn(b'Odoo sign-in failed',response.data)
        self.assertEqual(self.app.extensions['order_store'].listing(),[])

    def test_block_cancel_currency_mismatch_and_nonzero_deposit(self):
        h,r,p=records();h['state']='cancel';h['currency_id']=[2,'EUR'];h['amount_untaxed']=95
        order=order_from_records(h,r,p,CONFIG['ODOO_URL'],CONFIG['ODOO_DB'])
        self.assertEqual(len(order.issues),4)
        h,r,p=records();r[3]['product_uom_qty']=1;r[3]['price_subtotal']=50
        order=order_from_records(h,r,p,CONFIG['ODOO_URL'],CONFIG['ODOO_DB'])
        Mapper(self.seed).map_order(order)
        self.assertEqual(order.lines[3].status,'REVIEW')
        with self.assertRaises(ValueError):validate_export(order)

    def test_zero_and_negative_delivery(self):
        h,r,p=records();r[0]['qty_delivered']=0
        self.assertFalse(order_from_records(h,r,p,CONFIG['ODOO_URL'],CONFIG['ODOO_DB']).has_deliveries)
        r[0]['qty_delivered']=-1
        self.assertTrue(order_from_records(h,r,p,CONFIG['ODOO_URL'],CONFIG['ODOO_DB']).has_deliveries)

    def rpc(self, mutate=False, fault=False, count=1):
        h,r,p=records();calls=[]
        class Proxy:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def authenticate(self,*args):return 5
            def execute_kw(self,db,uid,key,model,method,args,kwargs):
                calls.append((model,method,args,kwargs))
                if fault:raise xmlrpc.client.Fault(1,CONFIG['ODOO_API_KEY'])
                if model=='sale.order' and method=='search_read':return [h]*count
                if model=='sale.order':return [dict(h,write_date='changed')] if mutate else [h]
                if model=='sale.order.line':return r
                if model=='product.product':return list(p.values())
                raise AssertionError(model)
        return Proxy,calls

    def test_rpc_uses_exact_search_read_only_and_preserves_codes(self):
        proxy,calls=self.rpc()
        with patch('odoo_api.xmlrpc.client.ServerProxy',side_effect=lambda *a,**k:proxy()):
            order=fetch_order(CONFIG,' S123 ')
        self.assertEqual(order.lines[0].code,'EXAMPLE')
        self.assertEqual(calls[0][2],[[['name','=','S123']]])
        self.assertTrue(all(c[1] in ('read','search_read') for c in calls))

    def test_rpc_fault_redaction_concurrent_changes_and_ambiguity(self):
        for kwargs in [dict(fault=True),dict(mutate=True),dict(count=2),dict(count=0)]:
            proxy,_=self.rpc(**kwargs)
            with patch('odoo_api.xmlrpc.client.ServerProxy',side_effect=lambda *a,**k:proxy()):
                with self.assertRaises(OdooError) as caught:fetch_order(CONFIG,'S123')
            self.assertNotIn(CONFIG['ODOO_API_KEY'],str(caught.exception))

    def test_configuration_https_authentication_and_timeout(self):
        for config in [{},dict(CONFIG,ODOO_URL='http://example.odoo.com'),dict(CONFIG,ODOO_URL='https://user:secret@example.odoo.com')]:
            with self.assertRaises(OdooError):fetch_order(config,'S123')
        with patch('odoo_api.xmlrpc.client.ServerProxy',side_effect=TimeoutError('private-test-key')):
            with self.assertRaises(OdooError) as caught:fetch_order(CONFIG,'S123')
        self.assertNotIn('private-test-key',str(caught.exception))
        proxy,_=self.rpc();proxy.authenticate=lambda *args:False
        with patch('odoo_api.xmlrpc.client.ServerProxy',side_effect=lambda *a,**k:proxy()):
            with self.assertRaises(OdooError):fetch_order(CONFIG,'S123')
