"""Read-only Odoo 18 XML-RPC order snapshots. Credentials never enter saved orders."""
import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit
import xmlrpc.client

from converter import Line, Order, ZERO, clean, money, number


class OdooError(ValueError):
    pass


class TimeoutTransport(xmlrpc.client.SafeTransport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = 20
        return connection


def configured(config):
    return all(config.get(k) for k in ('ODOO_URL', 'ODOO_DB', 'ODOO_USERNAME', 'ODOO_API_KEY'))


def _label(value):
    return clean(value[1]) if isinstance(value, (list, tuple)) and len(value) > 1 else ''


def order_from_records(header, rows, products, url, database):
    lines = []
    for n, row in enumerate(sorted(rows, key=lambda r: (r['sequence'], r['id'])), 1):
        note = bool(row.get('display_type'))
        qty = ZERO if note else number(row['product_uom_qty'])
        subtotal = ZERO if note else number(row['price_subtotal'])
        # Odoo's subtotal is net of discounts and excludes included taxes.
        # Deriving net unit price also accommodates Odoo's price computations.
        price = subtotal / qty if qty else (ZERO if note else number(row['price_unit']) * (1-number(row['discount'])/100))
        product = products.get(row['product_id'][0], {}) if row.get('product_id') else {}
        code = clean(product.get('default_code') or '')
        lines.append(Line(n=n, page=0, code=code, description=clean(row['name']), quantity=qty,
                          uom=_label(row.get('product_uom')), unit_price=price, amount=subtotal,
                          kind='note' if note else 'deposit' if row.get('is_downpayment') else 'item',
                          delivered_quantity=ZERO if note else number(row['qty_delivered'])))
    snapshot = {'header': header, 'lines': rows, 'products': products, 'url': url, 'database': database}
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    order = Order(order_id=header['name'], source_pdf='', sha256=digest,
                  customer_block=_label(header['partner_id']), date=clean(header['date_order']),
                  salesperson=_label(header['user_id']), untaxed=number(header['amount_untaxed']),
                  tax=number(header['amount_tax']), total=number(header['amount_total']), lines=lines,
                  source_type='odoo', imported_at=datetime.now(timezone.utc).isoformat(),
                  odoo_record_id=header['id'], odoo_state=header['state'])
    if not any(l.kind == 'item' for l in lines): order.issues.append('No order items found.')
    if money(order.source_sum) != money(order.untaxed):
        order.issues.append('Odoo line subtotals do not reconcile to the order subtotal.')
    if money(order.untaxed + order.tax) != money(order.total):
        order.issues.append('Odoo subtotal plus tax does not reconcile to the total.')
    if header['state'] == 'cancel': order.issues.append('This Odoo order is cancelled. Export is blocked.')
    if _label(header['currency_id']) != 'USD':
        order.issues.append('This order is not in USD. Currency conversion is not supported.')
    return order


def fetch_order(config, order_number):
    if not configured(config):
        raise OdooError('Set ODOO_URL, ODOO_DB, ODOO_USERNAME and ODOO_API_KEY in Portainer, then redeploy.')
    name = order_number.strip()
    if not name or len(name) > 100: raise OdooError('Enter an exact Odoo order number (up to 100 characters).')
    url = config['ODOO_URL'].strip().rstrip('/')
    parts = urlsplit(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path:
        raise OdooError('ODOO_URL must be an HTTPS base URL without a path or embedded credentials.')
    db, login, key = (config[k] for k in ('ODOO_DB', 'ODOO_USERNAME', 'ODOO_API_KEY'))
    try:
        with xmlrpc.client.ServerProxy(url+'/xmlrpc/2/common', transport=TimeoutTransport()) as common:
            uid = common.authenticate(db, login, key, {})
        if not uid: raise OdooError('Odoo sign-in failed. Check the database, API login and API key in Portainer.')
        with xmlrpc.client.ServerProxy(url+'/xmlrpc/2/object', transport=TimeoutTransport()) as models:
            def read(model, method, args, **kwargs):
                # There is intentionally no endpoint for arbitrary RPC methods or writes.
                if method not in ('read', 'search_read'): raise OdooError('Unsupported Odoo operation.')
                return models.execute_kw(db, uid, key, model, method, args, kwargs)
            headers = read('sale.order', 'search_read', [[['name', '=', name]]], limit=2,
                           fields=['name','partner_id','date_order','user_id','amount_untaxed','amount_tax',
                                   'amount_total','order_line','state','currency_id','write_date'])
            if not headers: raise OdooError('No accessible Odoo order matches that exact number. Check the number and user permissions.')
            if len(headers) != 1: raise OdooError('More than one order matches. Restrict the API user to the intended company before importing.')
            header = headers[0]
            ids = header['order_line']
            if len(ids) > 2000: raise OdooError('This order exceeds the 2,000-line import limit.')
            fields = ['sequence','name','product_id','product_uom_qty','product_uom','price_unit',
                      'discount','price_subtotal','qty_delivered','display_type','is_downpayment','write_date']
            rows = read('sale.order.line', 'read', [ids], fields=fields) if ids else []
            if {r['id'] for r in rows} != set(ids): raise OdooError('Some order lines are inaccessible. Check Odoo permissions.')
            product_ids = sorted({r['product_id'][0] for r in rows if r.get('product_id')})
            products = read('product.product', 'read', [product_ids], fields=['default_code']) if product_ids else []
            if {p['id'] for p in products} != set(product_ids): raise OdooError('Some products are inaccessible. Check Odoo permissions.')
            # Recheck both order and line versions to avoid mixing an order edited during import.
            again = read('sale.order', 'read', [[header['id']]], fields=['write_date','order_line'])
            line_versions = read('sale.order.line', 'read', [ids], fields=['write_date']) if ids else []
            if not again or again[0]['write_date'] != header['write_date'] or again[0]['order_line'] != ids or {r['id']:r['write_date'] for r in rows} != {r['id']:r['write_date'] for r in line_versions}:
                raise OdooError('The Odoo order changed during import. Try again to fetch a consistent snapshot.')
        return order_from_records(header, rows, {p['id']:p for p in products}, url, db)
    except OdooError:
        raise
    except xmlrpc.client.Fault:
        raise OdooError('Odoo rejected the read. Check API permissions for orders, order lines and products, and confirm Odoo 18 compatibility.') from None
    except (OSError, xmlrpc.client.Error):
        raise OdooError('Could not reach Odoo securely. Check the URL, server connection and API credentials, then retry.') from None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        raise OdooError('Odoo returned incomplete or unsupported order data. No order was imported.') from None
