from dataclasses import asdict
from decimal import Decimal as D
from io import BytesIO
from pathlib import Path
import csv
import json
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile
from fixtures import write_seed

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from converter import HEADERS, Line, Mapper, Order, money, number, parse_pdf, validate_export
from storage import decode_order, encode_order
from webapp import create_app
from xls_export import xls_bytes

ROOT=Path(__file__).resolve().parents[1]

def sample_pdf(total='1,751.67'):
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Table,TableStyle
    from reportlab.lib import colors
    out=BytesIO();c=canvas.Canvas(out,pagesize=(612,792));c.setFont('Helvetica',10)
    for y,text in [(755,'Example Metals'),(740,'100 Test Street'),(725,'Test City 00000'),(710,'United States'),(680,'Example Customer'),(650,'Order # TEST-001'),(630,'Order Date                             Salesperson'),(615,'10/06/2026                             Example User')]:
        c.drawString(36,y,text)
    rows=[['DESCRIPTION','QUANTITY','UNIT PRICE','AMOUNT'],
          ['[DEMOPOST] 6 x 6 x 18 Pressure Treated Post','2.00 Pcs','12.50','$ 25.00'],
          ['[T9ZZ2210] 29 Ga. Galvalume Tuff Rib (22\', 10")','40.00 Pcs','45.666667','$ 1,826.67'],
          ['[DEMOCREDIT500] DEMOCREDIT $500','1.00 Pcs','-100.00','$ -100.00'],
          ['[Down payment] Down payment','0.00 Pcs','250.00','']]
    table=Table(rows,colWidths=[298,75,78,89],rowHeights=[26]*5)
    table.setStyle(TableStyle([('GRID',(0,0),(-1,-1),.5,colors.black),('FONTNAME',(0,0),(-1,-1),'Helvetica'),('FONTSIZE',(0,0),(-1,-1),8),('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
    table.wrapOn(c,540,300);table.drawOn(c,36,460)
    c.drawString(370,430,'Untaxed Amount $ '+total)
    c.drawString(370,412,'Taxes $ 0.00')
    c.drawString(370,394,'Total $ '+total)
    c.save();return out.getvalue()

class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.pdf=sample_pdf()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.seed=write_seed(Path(self.tmp.name)/'test-seed')
        self.config={'TESTING':True,'DATA_DIR':str(Path(self.tmp.name)/'data'),'SEED_DIR':str(self.seed),'APP_PASSWORD':'testing-only-password'}
        self.app=create_app(self.config);self.client=self.app.test_client()
        self.client.get('/login');self.login()

    def token(self,client=None):
        with (client or self.client).session_transaction() as s:return s['csrf']

    def login(self,client=None):
        c=client or self.client;c.get('/login')
        return c.post('/login',data={'csrf_token':self.token(c),'password':self.config['APP_PASSWORD']})

    def upload(self,pdf=None):
        response=self.client.post('/upload',data={'csrf_token':self.token(),'pdfs':(BytesIO(pdf or self.pdf),'example.pdf')},content_type='multipart/form-data')
        self.assertEqual(response.status_code,302)
        key=response.location.rsplit('/',1)[-1]
        return key

    def current(self,key):return self.app.extensions['order_store'].get(key)

    def approve(self,key,n,pid,**changes):
        order,revision=self.current(key)
        line=next(l for l in order.lines if l.n==n)
        data={'csrf_token':self.token(),'revision':str(revision),'verified':'yes','remember':'yes','product_id':pid,'price_mode':line.price_mode,'feet':str(line.feet),'inches':str(line.inches),'multiplier':'1','cost':'','reviewer_note':''}
        data.update(changes)
        return self.client.post(f'/orders/{key}/lines/{n}',data=data)

    def finish(self,key):
        self.assertEqual(self.approve(key,2,'T9ZZ',price_mode='lf',feet='22',inches='10').status_code,302)
        self.assertEqual(self.approve(key,3,'DEMOCREDIT').status_code,302)

    def test_routes_upload_pdf_and_exact_total(self):
        key=self.upload();order,_=self.current(key)
        self.assertEqual(order.issues,[])
        self.assertEqual(order.source_sum,D('1751.67'))
        self.assertEqual(len(order.lines),4)
        self.assertEqual(len(order.pending),2)
        for path in ['/',f'/orders/{key}',f'/orders/{key}/lines/2','/catalog',f'/orders/{key}/review/html',f'/orders/{key}/review/csv']:
            r=self.client.get(path);self.assertEqual(r.status_code,200,path)
        with self.client.get(f'/orders/{key}/source') as response:
            self.assertEqual(response.data,self.pdf)

    def test_actual_xls_download_roundtrip(self):
        import xlrd
        key=self.upload();self.finish(key);order,revision=self.current(key)
        response=self.client.post(f'/orders/{key}/export/xls',data={'csrf_token':self.token(),'revision':revision,'verified':'yes'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data[:8],bytes.fromhex('d0cf11e0a1b11ae1'))
        s=xlrd.open_workbook(file_contents=response.data).sheet_by_index(0)
        self.assertEqual(s.row_values(0),HEADERS)
        self.assertEqual((s.nrows,s.ncols),(4,9))
        self.assertEqual(s.cell_value(2,0),'T9ZZ')
        self.assertEqual(s.row_values(2)[1:4],[22,10,40])
        self.assertEqual(s.cell_value(2,5),2.00000001)
        self.assertEqual(s.cell_value(3,5),-100)
        self.assertEqual(s.cell_value(2,6),'')
        self.assertNotIn('Down payment',str(s.row_values(3)))

    def test_incomplete_and_stale_exports_block(self):
        key=self.upload();_,revision=self.current(key)
        r=self.client.post(f'/orders/{key}/export/xls',data={'csrf_token':self.token(),'revision':revision,'verified':'yes'})
        self.assertEqual(r.status_code,409)
        self.finish(key)
        r=self.client.post(f'/orders/{key}/export/xls',data={'csrf_token':self.token(),'revision':revision,'verified':'yes'})
        self.assertEqual(r.status_code,409)

    def test_duplicate_upload_keeps_saved_review(self):
        key=self.upload();self.finish(key);_,revision=self.current(key)
        self.assertEqual(self.upload(),key)
        self.assertEqual(self.current(key)[1],revision)
        self.assertEqual(len(self.app.extensions['order_store'].listing()),1)

    def test_restart_keeps_order_reviews_and_mappings(self):
        key=self.upload();self.finish(key)
        new=create_app(self.config)
        order,revision=new.extensions['order_store'].get(key)
        self.assertEqual(len(order.pending),0)
        self.assertEqual(order.source_sum,D('1751.67'))
        self.assertEqual(revision,3)
        self.assertTrue((Path(self.config['DATA_DIR'])/'user_mappings.json').exists())

    def test_no_remember_still_saves_order_review(self):
        key=self.upload();self.approve(key,3,'DEMOCREDIT',remember='')
        new=create_app(self.config)
        order,_=new.extensions['order_store'].get(key)
        self.assertEqual(order.lines[2].status,'READY')
        self.assertFalse((Path(self.config['DATA_DIR'])/'user_mappings.json').exists())

    def test_csrf_and_auth_protect_data(self):
        key=self.upload()
        self.assertEqual(self.client.post('/upload',data={}).status_code,400)
        anonymous=self.app.test_client()
        self.assertEqual(anonymous.get(f'/orders/{key}/source').status_code,302)
        self.assertEqual(anonymous.get('/api/products?q=T9ZZ').status_code,401)
        self.assertEqual(anonymous.get('/healthz').status_code,200)

    def test_password_change_invalidates_session(self):
        new=create_app({**self.config,'APP_PASSWORD':'different-password-1234'})
        newer=new.test_client()
        cookie=self.client.get_cookie('session')
        newer.set_cookie('session',cookie.value)
        self.assertEqual(newer.get('/').status_code,302)

    def test_stale_review_does_not_overwrite(self):
        key=self.upload();_,revision=self.current(key)
        self.approve(key,3,'DEMOCREDIT')
        result=self.approve(key,3,'DEMOADJ2',revision=revision)
        self.assertEqual(result.status_code,409)
        self.assertEqual(self.current(key)[0].lines[2].product_id,'DEMOCREDIT')

    def test_catalog_update_invalidates_changed_specification(self):
        key=self.upload();self.finish(key)
        content=b'Product ID,Description,Unit Measure,Color,Category\nT9ZZ,Changed Galvanized Panel,LF,G-90 Galvanized,Panels\n'
        r=self.client.post('/catalog',data={'csrf_token':self.token(),'catalog':(BytesIO(content),'catalog.csv')},content_type='multipart/form-data')
        self.assertEqual(r.status_code,302)
        self.assertEqual(self.current(key)[0].lines[1].status,'REVIEW')
        self.assertTrue((Path(self.config['DATA_DIR'])/'catalog.before_update.json').exists())

    def test_source_total_mismatch_blocks(self):
        key=self.upload(sample_pdf('1,752.67'));self.finish(key)
        o,_=self.current(key)
        self.assertTrue(o.issues)
        with self.assertRaises(ValueError):validate_export(o)

    def test_interrupted_catalog_update_blocks_export_after_restart(self):
        key=self.upload();self.finish(key)
        content=b'Product ID,Description,Unit Measure\nTEST,Test product,EA\n'
        with patch('webapp.remap_order',side_effect=ValueError('Interrupted recheck')):
            r=self.client.post('/catalog',data={'csrf_token':self.token(),'catalog':(BytesIO(content),'catalog.csv')},content_type='multipart/form-data')
        self.assertEqual(r.status_code,400)
        self.app=create_app(self.config);self.client=self.app.test_client();self.login()
        _,revision=self.current(key)
        r=self.client.post(f'/orders/{key}/export/xls',data={'csrf_token':self.token(),'revision':revision,'verified':'yes'})
        self.assertEqual(r.status_code,409)
        self.assertIn(b'interrupted',r.data)

    def test_pdf_and_path_validation(self):
        r=self.client.post('/upload',data={'csrf_token':self.token(),'pdfs':(BytesIO(b'not a pdf'),'oops.pdf')},content_type='multipart/form-data',follow_redirects=True)
        self.assertIn(b'not a PDF',r.data)
        self.assertFalse(self.app.extensions['order_store'].listing())
        self.assertEqual(self.client.get('/orders/not-a-hash/source').status_code,404)

    def test_csv_discount_numeric(self):
        key=self.upload();self.finish(key);_,revision=self.current(key)
        r=self.client.post(f'/orders/{key}/export/csv',data={'csrf_token':self.token(),'revision':revision,'verified':'yes'})
        rows=list(csv.reader(r.data.decode('utf-8-sig').splitlines()))
        self.assertEqual(rows[0],HEADERS)
        self.assertEqual(rows[-1][5],'-100')

    def test_search_uses_p10_catalog(self):
        r=self.client.get('/api/products?q=DEMOFOREST')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json[0]['id'],'DEMOFOREST')

    def test_number_dimensions_and_finish_conflict(self):
        mapper=self.app.extensions['order_mapper']
        for x in ('NaN','Infinity'):
            with self.assertRaises(ValueError):number(x)
        line=Line(1,1,'DEMOCLASH','Ridge Cap 29 Ga. (10\' 4", Galvalume)',D('6'),'Pcs',D('10.00'),D('60.00'))
        mapper.map_line(line)
        self.assertEqual(line.status,'REVIEW')
        with self.assertRaises(ValueError):mapper.approve(line,'DEMOCLASH',feet='10',inches='4')
        with self.assertRaises(ValueError):mapper.approve(line,'DEMOTRIM',feet='10',inches='12')

    def test_screw_bags_stay_bags_and_stock_cutoff_not_used(self):
        mapper=self.app.extensions['order_mapper']
        line=Line(1,1,'DEMOSCREW','PTD Painted Screws, Metal to Wood, Bag of 250 (1.5", Galvalume)',D('10'),'Bag of 250',D('5.00'),D('50.00'))
        mapper.map_line(line)
        self.assertEqual(line.product_id,'DEMOBAG')
        self.assertEqual(line.target_quantity,D('10'))
        self.assertEqual(line.status,'READY')

    def test_compose_configuration_and_private_runtime_exclusions(self):
        import yaml
        for name in ('compose.yaml','portainer-stack.yml'):
            d=yaml.safe_load((ROOT/name).read_text());s=d['services']['converter']
            self.assertIn('converter_data:/data',s['volumes'])
            self.assertIn('8091',s['ports'][0])
            self.assertTrue(s['read_only'])
        self.assertIn('runtime',(ROOT/'.dockerignore').read_text())

    def test_removed_upload_keeps_product_mappings(self):
        key=self.upload();self.finish(key);_,rev=self.current(key)
        r=self.client.post(f'/orders/{key}/delete',data={'csrf_token':self.token(),'revision':rev,'confirm':'delete'})
        self.assertEqual(r.status_code,302)
        self.assertIsNone(self.current(key))
        self.assertFalse((Path(self.config['DATA_DIR'])/'uploads'/f'{key}.pdf').exists())
        self.assertTrue((Path(self.config['DATA_DIR'])/'user_mappings.json').exists())

    def setup_zip(self, extra=None):
        output=BytesIO()
        with ZipFile(output,'w') as archive:
            for path in self.seed.iterdir():archive.writestr('private-project/seed/'+path.name,path.read_bytes())
            archive.writestr('private-project/should-not-run.py','raise RuntimeError("Do not execute")')
            if extra:archive.writestr(*extra)
        output.seek(0);return output

    def test_empty_image_setup_import_and_restart(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            config={**self.config,'DATA_DIR':directory,'SEED_DIR':str(Path(directory)/'absent-seed')}
            self.app=create_app(config);self.client=self.app.test_client();self.login()
            self.assertEqual(len(self.app.extensions['order_mapper'].catalog),0)
            self.assertIn(b'Set up your project first',self.client.get('/').data)
            response=self.client.post('/upload',data={'csrf_token':self.token(),'pdfs':(BytesIO(self.pdf),'test.pdf')})
            self.assertEqual(response.status_code,400)
            response=self.client.post('/setup',data={'csrf_token':self.token(),'confirmed':'yes','setup_zip':(self.setup_zip(),'private.zip')})
            self.assertEqual(response.status_code,302)
            self.assertEqual(len(self.app.extensions['order_mapper'].catalog),8)
            self.assertFalse((Path(directory)/'should-not-run.py').exists())
            key=self.upload();self.finish(key)
            self.assertFalse(create_app(config).extensions['order_store'].get(key)[0].pending)

    def test_setup_rejects_duplicate_or_invalid_data_and_requires_confirmation(self):
        from project_setup import read_setup
        with self.assertRaises(ValueError):read_setup(self.setup_zip(('other/seed/catalog.json','{}')))
        output=BytesIO()
        with ZipFile(output,'w') as archive:
            archive.writestr('seed/catalog.json','{}');archive.writestr('seed/project_mappings.json','{}')
        output.seek(0)
        with self.assertRaises(ValueError):read_setup(output)
        response=self.client.post('/setup',data={'csrf_token':self.token(),'setup_zip':(self.setup_zip(),'private.zip')})
        self.assertEqual(response.status_code,400)
        anonymous=self.app.test_client()
        self.assertEqual(anonymous.get('/setup').status_code,302)

    def test_docker_does_not_include_private_seed(self):
        self.assertNotIn('COPY --chown=converter:converter seed', (ROOT/'Dockerfile').read_text())
        self.assertIn('seed/', (ROOT/'.gitignore').read_text())

if __name__=='__main__':unittest.main()
