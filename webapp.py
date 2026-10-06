from __future__ import annotations
from dataclasses import asdict
from datetime import timedelta
from functools import wraps
from io import BytesIO, StringIO
import csv
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from converter import HEADERS, Mapper, clean, decstr, money, p10_rows, parse_pdf, review_rows, REVIEW_HEADERS, safe_cell, save_review, validate_export
from catalog_update import update_catalog
from project_setup import read_setup, install_setup
from storage import Store, decode_order, remap_order
from xls_export import xls_bytes

ROOT = Path(__file__).resolve().parent

def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        DATA_DIR=os.environ.get('DATA_DIR',str(ROOT/'runtime')),
        SEED_DIR=str(ROOT/'seed'),
        APP_PASSWORD=os.environ.get('APP_PASSWORD',''),
        MAX_CONTENT_LENGTH=32*1024*1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE','false').lower()=='true',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    if config: app.config.update(config)
    password=app.config['APP_PASSWORD']
    if not password or len(password)<12:
        raise RuntimeError('Set APP_PASSWORD to a password of at least 12 characters in Portainer.')
    store=Store(app.config['DATA_DIR'],app.config['SEED_DIR'])
    key_path=store.root/'session.key'
    if not key_path.exists():
        with key_path.open('x') as handle: handle.write(secrets.token_hex(32))
        key_path.chmod(0o600)
    # Changing the login password invalidates old sessions too.
    app.secret_key=hmac.new(key_path.read_bytes(),password.encode(),hashlib.sha256).digest()
    mapper=Mapper(store.root)
    catalog_pending=store.root/'catalog_update_pending'
    app.extensions.update(order_store=store,order_mapper=mapper)
    failed_logins={}

    def csrf_token():
        if 'csrf' not in session: session['csrf']=secrets.token_urlsafe(32)
        return session['csrf']

    app.jinja_env.globals.update(csrf_token=csrf_token,decstr=decstr)
    app.jinja_env.filters['money']=lambda n: '' if n is None else f'{money(n):,.2f}'

    @app.before_request
    def csrf_check():
        if request.method=='POST':
            supplied=request.form.get('csrf_token','')
            if not supplied or not hmac.compare_digest(supplied,session.get('csrf','')):
                abort(400,description='Session form token expired. Reload the page and try again.')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        response.headers['Cache-Control']='no-store'
        return response

    def login_required(fn):
        @wraps(fn)
        def wrapped(*args,**kwargs):
            if not session.get('signed_in'):
                if request.path.startswith('/api/'): abort(401)
                return redirect(url_for('login'))
            return fn(*args,**kwargs)
        return wrapped

    def get_order(key):
        if not re.fullmatch('[0-9a-f]{64}',key): abort(404)
        result=store.get(key)
        if not result: abort(404)
        return result

    @app.get('/healthz')
    def health():
        with store.connect() as db: db.execute('SELECT 1').fetchone()
        return jsonify(status='ok',version='2.0.1')

    @app.route('/login',methods=['GET','POST'])
    def login():
        if request.method=='POST':
            ip=request.remote_addr or 'unknown'
            now=time.monotonic()
            with store.lock:
                failures=[t for t in failed_logins.get(ip,[]) if now-t<300]
                if len(failures)>=10: abort(429,description='Too many attempts. Wait five minutes and try again.')
                if not hmac.compare_digest(request.form.get('password','').encode(),password.encode()):
                    failed_logins[ip]=failures+[now]
                    flash('That password did not match.','error')
                    return render_template('login.html'),401
                failed_logins.pop(ip,None)
            session.clear();session['signed_in']=True;session.permanent=True;csrf_token()
            return redirect(url_for('index'))
        return render_template('login.html')

    @app.post('/logout')
    @login_required
    def logout():
        session.clear();return redirect(url_for('login'))

    @app.get('/')
    @login_required
    def index():
        orders=[]
        for row in store.listing():
            o=decode_order(row['payload'])
            orders.append({'key':row['id'],'order':o,'updated':row['updated_at'],'ready':not o.pending and not o.issues})
        return render_template('index.html',orders=orders,product_count=len(mapper.catalog),mapping_count=len(mapper.rules))

    @app.post('/upload')
    @login_required
    def upload():
        if not mapper.catalog or not mapper.rules:
            abort(400,description='Load the private project setup ZIP from the Catalog page before uploading orders.')
        uploaded=request.files.getlist('pdfs')
        if not uploaded or len(uploaded)>20: abort(400,description='Choose between 1 and 20 PDF files.')
        first=None
        for item in uploaded:
            label=Path(item.filename or 'file').name
            if not label.lower().endswith('.pdf'):
                flash(f'{label}: select an Odoo PDF.','error');continue
            raw=item.read()
            if not raw.startswith(b'%PDF-'):
                flash(f'{label}: this is not a PDF file.','error');continue
            key=hashlib.sha256(raw).hexdigest()
            with store.lock:
                if store.get(key):
                    first=first or key;flash(f'{label}: this PDF is already in the order list.');continue
                target=store.uploads/(key+'.pdf')
                try:
                    import pdfplumber
                    with pdfplumber.open(BytesIO(raw)) as check:
                        if len(check.pages)>100: raise ValueError('Maximum 100 pages per PDF.')
                    target.write_bytes(raw)
                    order=mapper.map_order(parse_pdf(target))
                    store.insert(order);first=first or key
                    flash(f'{order.order_id}: {len(order.pending)} lines need review.')
                except Exception as exc:
                    target.unlink(missing_ok=True)
                    flash(f'{label}: {str(exc)[:400]}','error')
        return redirect(url_for('order_detail',key=first) if first and len(uploaded)==1 else url_for('index'))

    @app.get('/orders/<key>')
    @login_required
    def order_detail(key):
        order,revision=get_order(key)
        return render_template('order.html',order=order,key=key,revision=revision,can_export=not order.pending and not order.issues)

    @app.route('/orders/<key>/lines/<int:line_no>',methods=['GET','POST'])
    @login_required
    def edit_line(key,line_no):
        with store.lock:
            order,revision=get_order(key)
            line=next((l for l in order.lines if l.n==line_no),None)
            if not line or line.kind!='item':abort(404)
            if request.method=='POST':
                if request.form.get('revision')!=str(revision):abort(409,description='This order changed in another tab. Reload before saving.')
                if request.form.get('verified')!='yes':abort(400,description='Verify the product, dimensions, units and price basis before applying.')
                fields={k:request.form.get(k,'') for k in ('product_id','price_mode','feet','inches','multiplier','cost','reviewer_note')}
                try:
                    mapper.approve(line,**fields,remember=request.form.get('remember')=='yes')
                    store.update(order,revision)
                    flash(f'Line {line.n} reviewed and saved.')
                    return redirect(url_for('order_detail',key=key)+'#line-'+str(line.n))
                except (ValueError,ArithmeticError) as exc:
                    flash(str(exc),'error')
                    return render_template('line.html',order=order,key=key,line=line,revision=revision),400
        return render_template('line.html',order=order,key=key,line=line,revision=revision)

    @app.get('/api/products')
    @login_required
    def products():
        query=request.args.get('q','')[:120]
        with store.lock:
            results=mapper.search(query,60)
        return jsonify([{'id':p['id'],'description':p['description'],'uom':p['uom'],'color':p.get('color','')} for p in results])

    @app.post('/orders/<key>/export/<fmt>')
    @login_required
    def export(key,fmt):
        with store.lock:
            if catalog_pending.exists():
                abort(409,description='A product-data update was interrupted. Upload the project setup ZIP again to finish rechecking orders before exporting.')
            order,revision=get_order(key)
            if request.form.get('revision')!=str(revision):abort(409,description='Order changed. Reload the review before exporting.')
            if request.form.get('verified')!='yes':abort(400,description='Confirm the remaining order quantities and destination order before exporting.')
            if fmt not in ('xls','csv'):abort(404)
            try:
                validate_export(order)
                if fmt=='xls':
                    content=xls_bytes(order);mime='application/vnd.ms-excel'
                else:
                    rows=p10_rows(order)
                    if any(str(row[0]).lstrip().startswith(('=','+','-','@')) for row in rows):
                        raise ValueError('Use XLS to preserve ProductIDs that begin with a formula character.')
                    data=StringIO(newline='');writer=csv.writer(data);writer.writerow(HEADERS)
                    writer.writerows([[safe_cell(v) for v in row] for row in rows])
                    content=data.getvalue().encode('utf-8-sig');mime='text/csv'
            except ValueError as exc:abort(409,description=str(exc))
        name=re.sub(r'[^A-Za-z0-9_-]','_',order.order_id)+'_P10.'+fmt
        return send_file(BytesIO(content),mimetype=mime,as_attachment=True,download_name=name)

    @app.get('/orders/<key>/source')
    @login_required
    def source(key):
        order,_=get_order(key)
        return send_file(order.source_pdf,mimetype='application/pdf',as_attachment=True,download_name=re.sub(r'[^A-Za-z0-9_-]','_',order.order_id)+'.pdf')

    @app.get('/orders/<key>/review/<fmt>')
    @login_required
    def review(key,fmt):
        order,_=get_order(key)
        if fmt not in ('html','csv','json'):abort(404)
        with tempfile.TemporaryDirectory() as tmp:
            html_path=save_review(order,tmp)
            target=html_path.with_suffix('.'+fmt)
            content=target.read_bytes()
        return send_file(BytesIO(content),mimetype={'html':'text/html','csv':'text/csv','json':'application/json'}[fmt],as_attachment=fmt!='html',download_name=target.name)

    @app.route('/setup',methods=['GET','POST'])
    @login_required
    def setup():
        if request.method=='POST':
            if request.form.get('confirmed')!='yes':
                abort(400,description='Confirm replacement of the project catalog and rules.')
            upload=request.files.get('setup_zip')
            if not upload or not (upload.filename or '').lower().endswith('.zip'):
                abort(400,description='Choose the private converter ZIP containing the seed files.')
            try:
                data=read_setup(upload.stream)
                with store.lock:
                    catalog_pending.touch()
                    install_setup(data,store.root)
                    mapper.catalog=data['catalog.json'];mapper.rules=data['project_mappings.json']
                    for row in store.listing():
                        order,revision=get_order(row['id']);remap_order(order,mapper);store.update(order,revision)
                    catalog_pending.unlink()
                flash(f'Loaded {len(mapper.catalog):,} products and {len(mapper.rules):,} project rules. Orders were rechecked.')
                return redirect(url_for('index'))
            except Exception as exc:
                flash(str(exc)[:500],'error')
                return render_template('setup.html'),400
        return render_template('setup.html')

    @app.route('/catalog',methods=['GET','POST'])
    @login_required
    def catalog():
        if request.method=='POST':
            item=request.files.get('catalog')
            suffix=Path(item.filename or '').suffix.lower() if item else ''
            if suffix not in ('.xls','.xlsx','.csv'):abort(400,description='Choose a P10 XLS, XLSX or CSV catalog.')
            with tempfile.TemporaryDirectory() as tmp:
                target=Path(tmp)/('P10_catalog'+suffix);item.save(target)
                try:
                    with store.lock:
                        # Keep exports blocked across restarts if any recheck fails.
                        catalog_pending.touch()
                        count=update_catalog(mapper,target)
                        for row in store.listing():
                            o,rev=get_order(row['id']);remap_order(o,mapper);store.update(o,rev)
                        catalog_pending.unlink()
                    flash(f'Updated {count:,} products. Orders were rechecked; unsaved mapping approvals reset.')
                except Exception as exc:
                    flash(str(exc)[:500],'error')
                    return render_template('catalog.html',product_count=len(mapper.catalog)),400
            return redirect(url_for('catalog'))
        return render_template('catalog.html',product_count=len(mapper.catalog))

    @app.post('/orders/<key>/delete')
    @login_required
    def delete_order(key):
        with store.lock:
            order,revision=get_order(key)
            if request.form.get('confirm')!='delete' or request.form.get('revision')!=str(revision):
                abort(409,description='Reload the order and confirm deletion.')
            store.delete(key)
        flash(f'{order.order_id} removed from this converter. ERP data was not changed.')
        return redirect(url_for('index'))

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def problem(exc):
        return render_template('error.html',message=exc.description,code=exc.code),exc.code

    return app

if __name__=='__main__':
    from waitress import serve
    serve(create_app(),host='0.0.0.0',port=8080,threads=4,max_request_body_size=32*1024*1024)
