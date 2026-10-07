"""Local, review-first Odoo PDF to Paradigm detail converter. No ERP/network writes."""
from __future__ import annotations
import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HEADERS = ['ProductID','LinearAmount1','LinearAmount2','PcsOrdered','Comment','SalesPrice','Cost','Color','Description']
ZERO = Decimal('0')
CENT = Decimal('.01')
NUM = r'[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'

def clean(value):
    text = str('' if value is None else value).translate(str.maketrans({'\u200b':'','\u200c':'','\ufeff':'','\u2212':'-','\u00a0':' ','’':"'",'′':"'",'″':'"','“':'"','”':'"'}))
    return text.strip()

def normal(value): return ' '.join(clean(value).upper().split())

def number(value):
    s = clean(value).replace('$','').replace(',','').replace(' ','')
    if s.startswith('(') and s.endswith(')'): s = '-' + s[1:-1]
    try: n = Decimal(s)
    except InvalidOperation as e: raise ValueError(f'Not a number: {value!r}') from e
    if not n.is_finite(): raise ValueError('Number must be finite.')
    return n

def money(n): return n.quantize(CENT, rounding=ROUND_HALF_UP)

def decstr(n):
    if n is None: return ''
    if not isinstance(n,Decimal): return str(n)
    s = format(n,'f')
    return (s.rstrip('0').rstrip('.') if '.' in s else s) or '0'

def length(text):
    """Only explicit feet/inches. Never interpret lumber/truss dimensions as a cut."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*'\s*,?\s*(\d+(?:\.\d+)?)\s*\"", clean(text))
    if not m: return ZERO, ZERO
    return number(m[1]), number(m[2])

def scope_key(line):
    return hashlib.sha256((normal(line.code)+'\n'+normal(line.description)+'\n'+normal(line.uom)).encode()).hexdigest()

@dataclass
class Line:
    n: int
    page: int
    code: str
    description: str
    quantity: Decimal
    uom: str
    unit_price: Decimal
    amount: Decimal
    kind: str = 'item'
    product_id: str = ''
    p10_description: str = ''
    p10_uom: str = ''
    color: str = ''
    feet: Decimal = ZERO
    inches: Decimal = ZERO
    multiplier: Decimal = Decimal('1')
    price_mode: str = 'piece'
    cost: str = ''
    basis: str = ''
    source: str = ''
    issues: list[str] = field(default_factory=list)
    approved: bool = False
    reviewer_note: str = ''
    review_mode: str = ''
    delivered_quantity: Decimal | None = None

    @property
    def mi_com_code(self): return 'MI' if self.unit_price != ZERO or self.amount != ZERO else 'COM'

    @property
    def cut_length(self): return self.feet + self.inches / Decimal('12')
    @property
    def target_quantity(self):
        if self.review_mode == 'mi_com' and self.product_id == 'COM': return ZERO
        return self.quantity * self.multiplier
    @property
    def target_price(self):
        if self.review_mode == 'mi_com' and self.product_id == 'COM': return ZERO
        divisor = self.multiplier * (self.cut_length if self.price_mode == 'lf' else Decimal('1'))
        if divisor <= 0: raise ValueError(f'Line {self.n}: missing length or invalid multiplier.')
        return (self.unit_price/divisor).quantize(Decimal('.00000001'),rounding=ROUND_HALF_UP)
    @property
    def target_amount(self):
        return money(self.target_quantity*self.target_price*(self.cut_length if self.price_mode == 'lf' else Decimal('1')))
    @property
    def status(self):
        if self.kind == 'deposit' and self.quantity == ZERO and self.amount == ZERO: return 'REFERENCE'
        if self.review_mode == 'mi_com': return 'READY' if self.approved and not self.issues else 'REVIEW'
        if self.kind == 'note': return 'NOTE'
        return 'READY' if self.approved and not self.issues else 'REVIEW'

@dataclass
class Order:
    order_id: str
    source_pdf: str
    sha256: str
    customer_block: str
    date: str
    salesperson: str
    untaxed: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    lines: list[Line]
    issues: list[str] = field(default_factory=list)
    page_count: int = 0
    source_type: str = 'pdf'
    imported_at: str = ''
    odoo_record_id: int | None = None
    odoo_state: str = ''

    @property
    def has_deliveries(self):
        return any(l.delivered_quantity is not None and l.delivered_quantity != ZERO for l in self.lines)

    @property
    def source_sum(self): return sum((l.amount for l in self.lines if l.kind != 'note'),ZERO)
    @property
    def pending(self): return [l for l in self.lines if l.status == 'REVIEW']

def parse_pdf(path):
    import pdfplumber
    path = Path(path)
    lines, errors, texts, bounds = [], [], [], None
    raw = path.read_bytes()
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        if not pdf.pages: raise ValueError('The PDF has no readable pages.')
        for page_no,page0 in enumerate(pdf.pages,1):
            page = page0.crop((0,0,page0.width,page0.height)).dedupe_chars(tolerance=1)
            text = clean(page.extract_text() or '')
            texts.append(text)
            if not text: errors.append(f'Page {page_no} has no text. Scanned PDFs need a text/OCR export.'); continue
            # Recover actual column boundaries from the supplied Odoo header.
            if bounds is None:
                for table in page.find_tables():
                    for row,cells in zip(table.rows,table.extract()):
                        if [normal(x) for x in cells] == ['DESCRIPTION','QUANTITY','UNIT PRICE','AMOUNT']:
                            bounds = sorted({v for cell in row.cells if cell for v in (cell[0],cell[2])})
                            break
                    if bounds: break
                if bounds is None:
                    raise ValueError('Unsupported PDF layout: expected DESCRIPTION / QUANTITY / UNIT PRICE / AMOUNT. Export the original Odoo order PDF, not a scan or a tax/discount-column variant.')
            tables = page.extract_tables({'vertical_strategy':'explicit','explicit_vertical_lines':bounds})
            parsed_count = 0
            for table in tables:
                for row in table:
                    if len(row)!=4:
                        errors.append(f'Page {page_no}: unexpected table width.'); continue
                    desc,qty,rate,amount = [clean(c) for c in row]
                    if normal(desc)=='DESCRIPTION' or not any((desc,qty,rate,amount)): continue
                    if 'UNTAXED' in normal(desc) or normal(desc).startswith(('TAXES','TOTAL','TERMS & CONDITIONS')): break
                    m = re.fullmatch(rf'({NUM})\s+(.+)', ' '.join(qty.split()))
                    if not m:
                        if rate or amount or qty:
                            errors.append(f'Page {page_no}: unreadable quantity or unexpected row: {desc[:80]} {qty} {rate} {amount}')
                        elif desc:
                            lines.append(Line(len(lines)+1,page_no,'',desc,ZERO,'',ZERO,ZERO,kind='note',approved=True))
                        continue
                    if not desc:
                        errors.append(f'Page {page_no}: priced row has no description.'); continue
                    try:
                        q,price = number(m[1]),number(rate) if rate else ZERO
                        unit = ' '.join(m[2].split())
                        deposit = 'DOWN PAYMENT' in normal(desc)
                        if amount: a=number(amount)
                        elif price == ZERO or (q == ZERO and deposit): a=ZERO
                        else: raise ValueError('Missing printed line amount')
                    except ValueError as e:
                        errors.append(f'Page {page_no}: {desc[:60]}: {e}'); continue
                    c = re.match(r'^\[([^\]]+)\]\s*(.*)',desc,re.S)
                    code,description = (c[1],c[2]) if c else ('',desc)
                    line=Line(len(lines)+1,page_no,code,description,q,unit,price,a,kind='deposit' if deposit else 'item')
                    if money(q*price)!=money(a):
                        errors.append(f'Line {line.n}: quantity x unit price {money(q*price)} differs from printed amount {a}. Discounts/taxes or extraction need review.')
                    lines.append(line); parsed_count+=1
            # Independent count of quantity-leading text lines catches missed zero-dollar lines too.
            quantity_words = page.crop((bounds[1]+1,0,bounds[2]-1,page.height)).extract_text() or ''
            expected = len(re.findall(rf'^\s*{NUM}\s+(?:Pcs|Bag\s+of|EA|Units?|LF|Ft|Feet|Box(?:es)?|Set(?:s)?|Roll(?:s)?)\b',clean(quantity_words),re.M|re.I))
            if expected != parsed_count:
                errors.append(f'Page {page_no}: quantity-column count {expected} differs from extracted item count {parsed_count}.')
        all_text = '\n'.join(texts)
        ids = set(re.findall(r'(?:Order|Quotation)\s*#\s*([A-Za-z0-9_-]+)',all_text,re.I))
        if len(ids)!=1: errors.append('Expected exactly one Odoo order number per PDF.')
        oid = next(iter(ids)) if len(ids)==1 else path.stem
        def total_for(label):
            values = re.findall(rf'^\s*{label}\s+\$?\s*({NUM})\s*$',all_text,re.M|re.I)
            if len(values)!=1: errors.append(f'Cannot uniquely read {label}.'); return None
            return number(values[0])
        untaxed,tax,total = total_for('Untaxed Amount'),total_for('Taxes'),total_for('Total')
        date = re.search(r'\b\d{2}/\d{2}/\d{4}\b',texts[0])
        person = re.search(r'\b\d{2}/\d{2}/\d{4}\s+([^\n]+)',texts[0])
        header = texts[0].split('Order #')[0].split('Quotation #')[0]
        customer = '\n'.join(header.splitlines()[4:]).strip()
        order=Order(oid,str(path.resolve()),hashlib.sha256(raw).hexdigest(),customer,date[0] if date else '',person[1] if person else '',untaxed,tax,total,lines,errors,len(pdf.pages))
    if not any(l.kind=='item' for l in lines): order.issues.append('No order items found.')
    if untaxed is not None and money(order.source_sum)!=money(untaxed):
        order.issues.append(f'Extracted line total {money(order.source_sum)} differs from source untaxed total {untaxed}.')
    if None not in (untaxed,tax,total) and money(untaxed+tax)!=money(total):
        order.issues.append('PDF subtotal plus tax does not equal total.')
    return order

def finish(text):
    t = normal(text)
    if 'GALVALUME' in t: return 'GALVALUME'
    if 'GALVANIZED' in t or 'G-90' in t: return 'GALVANIZED'
    return ''

def conflicts(line,product):
    out=[]
    a,b=finish(line.description),finish(product['description'])
    if a and b and a!=b: out.append(f'Finish conflict: Odoo {a}; P10 {b}.')
    # Product code equality does not override the physical cut length or packaging.
    if product.get('category','').upper() in ('TRIM','PANELS'):
        lf,li=length(line.description); pf,pi=length(product['description'])
        if lf and pf and (lf,li)!=(pf,pi): out.append(f'Cut-length conflict: Odoo {lf} ft {li} in; P10 {pf} ft {pi} in.')
    if product.get('discontinued'): out.append('P10 catalog marks this item discontinued.')
    return out

class Mapper:
    def __init__(self,data_dir=None):
        self.data_dir=Path(data_dir or ROOT/'seed')
        self.catalog=json.loads((self.data_dir/'catalog.json').read_text(encoding='utf-8'))
        self.rules=json.loads((self.data_dir/'project_mappings.json').read_text(encoding='utf-8'))
        self.overrides_path=self.data_dir/'user_mappings.json'
        self.overrides=json.loads(self.overrides_path.read_text(encoding='utf-8')) if self.overrides_path.exists() else {}

    def search(self,query,limit=100):
        terms=normal(query).split()
        if not terms: return []
        matches=[p for p in self.catalog.values() if all(t in normal(p['id']+' '+p['description']) for t in terms)]
        return sorted(matches,key=lambda p:(normal(p['id'])!=normal(query),not p.get('hopkinsville'),len(p['id']),p['id']))[:limit]

    def _family(self,line):
        code=normal(line.code)
        # Reuse an already reviewed trim family for a different explicit stock length.
        if re.fullmatch(r'[A-Z0-9]+\d{2}',code):
            prefixes=set()
            for key,r in self.rules.items():
                pid=normal(r.get('product_id',''))
                if r.get('approved') and key[:-2]==code[:-2] and key[-2:].isdigit() and pid[-2:].isdigit() and key[-2:]==pid[-2:]:
                    prefixes.add(pid[:-2])
            if len(prefixes)==1:
                p=self.catalog.get(next(iter(prefixes))+code[-2:])
                if p and p.get('category','').upper()=='TRIM' and length(line.description)==length(p['description']) and length(line.description)[0]>0 and not conflicts(line,p):
                    color=normal(p.get('color',''))
                    if color and color in normal(line.description):
                        return {'product_id':p['id'],'approved':True,'basis':'Reviewed trim family; P10 color and full cut length match.','source':'Project mapping family + P10 catalog'}
        # New lengths of known Tuff Rib products: use the stated gauge/finish, not GV code suffix.
        if re.fullmatch(r'T[69][A-Z]{2}\d{4,}',normal(line.code)) and 'TUFF RIB' in normal(line.description):
            gauge=re.search(r'\b(26|29)\s*GA',normal(line.description))
            if not gauge: return None
            desc=normal(line.description)
            colors={normal(p.get('color','')) for p in self.catalog.values() if p.get('color')}
            stated=[c for c in colors if c in desc]
            if 'GALVALUME' in desc: stated=['GALVALUME']
            if 'SIERRA TAN' in desc: stated=['STONE']
            if not stated: return None
            color=max(stated,key=len)
            candidates=[p for p in self.catalog.values() if re.fullmatch('T'+gauge[1][-1]+r'[A-Z]{2}',p['id']) and 'TUFF RIB' in normal(p['description']) and normal(p.get('color'))==color and p['uom'].upper()=='LF']
            if len(candidates)==1:
                return {'product_id':candidates[0]['id'],'approved':True,'basis':'Tuff Rib gauge and stated color match a unique P10 panel. Length retained from PDF.','source':'P10 catalog specifications'}
        return None

    def map_order(self,order):
        for line in order.lines: self.map_line(line)
        return order

    def map_line(self,line):
        if line.kind=='note': return
        if line.kind=='deposit':
            line.approved=False
            line.issues=[] if line.quantity==ZERO and line.amount==ZERO else ['Nonzero down payment requires separate accounting handling; not a product import.']
            line.basis='Down payment is an accounting reference, not a new charge or payment posting.'
            return
        override=self.overrides.get(scope_key(line))
        if override:
            # Apply only if current source and target specifications still agree with saved approval.
            p=self.catalog.get(normal(override.get('product_id','')))
            if p and override.get('catalog_signature')==self.signature(p):
                self.approve(line,remember=False,**{k:v for k,v in override.items() if k in ('product_id','price_mode','feet','inches','multiplier','cost','reviewer_note')})
                line.source='Saved user mapping'; return
        code=normal(line.code)
        rule=self.rules.get(code)
        if not code:
            # Some PDFs print the identifier as a prefix without square brackets.
            first=normal(line.description.split()[0]) if line.description.split() else ''
            if first=='PAL' and first in self.catalog and 'PALLET' in normal(line.description):
                rule={'product_id':'PAL','approved':True,'basis':'Explicit PAL prefix and matching pallet description.','source':'P10 catalog'}
        if not rule: rule=self._family(line)
        if not rule and code in self.catalog:
            rule={'product_id':self.catalog[code]['id'],'approved':True,'basis':'Exact P10 product code; specification checks applied.','source':self.catalog[code]['source']}
        line.issues=[]
        line.approved=False
        if not rule:
            line.basis='No reviewed code mapping.'
            line.issues=['Choose a P10 product.']; return
        line.basis=rule['basis']; line.source=rule['source']
        p=self.catalog.get(normal(rule.get('product_id','')))
        if not p:
            line.issues=['No verified P10 product: '+line.basis]; return
        self.apply_product(line,p)
        line.feet,line.inches=length(line.description) if p['category'].upper() in ('PANELS','TRIM') or p['uom'].upper()=='LF' else (ZERO,ZERO)
        line.price_mode='lf' if p['uom'].upper()=='LF' else 'piece'
        line.issues.extend(conflicts(line,p))
        if not rule.get('approved'): line.issues.append('Mapping held for review: '+line.basis)
        if p['uom'].upper()=='LF':
            line.issues.append('Confirm P10 SalesPrice is per LF and pieces + feet/inches are used for this product.')
            if line.cut_length<=ZERO: line.issues.append('Enter a positive cut length for LF conversion.')
        elif p['uom'].upper() not in ('EA','EACH','PC','PCS'):
            line.issues.append('Confirm P10 unit conversion: '+p['uom'])
        if 'BAG' in normal(line.uom) and not rule.get('source_uom') and 'BAG' not in normal(p['description']):
            line.issues.append('Confirm package size and quantity multiplier.')
        if not line.issues:
            line.approved=True
        self.validate_line(line)

    def apply_product(self,line,p):
        line.product_id=p['id'];line.p10_description=p['description'];line.p10_uom=p['uom'];line.color=p.get('color','')

    def approve_mi_com(self,line):
        """Explicit order-only review choice; never creates a reusable product alias."""
        if line.kind not in ('item','note'):
            raise ValueError('Down-payment references are handled separately, not as MI/COM products.')
        code=line.mi_com_code
        product=self.catalog.get(code)
        if not product or product.get('discontinued'):
            raise ValueError(f'P10 {code} must be present and active in the loaded catalog.')
        if code=='MI' and product['uom'].upper() not in ('EA','EACH','PC','PCS'):
            raise ValueError('P10 MI must use a piece/each unit for this conversion.')
        if money(line.quantity*line.unit_price)!=money(line.amount):
            raise ValueError(f'Line {line.n}: quantity × unit price does not match the PDF amount.')
        # MI keeps the source quantity and piece/package price; COM has no charge.
        self.apply_product(line,product)
        line.review_mode='mi_com'
        line.feet=line.inches=ZERO
        line.multiplier=Decimal('1');line.price_mode='piece';line.cost='';line.color=''
        line.issues=[];line.approved=True
        line.basis=f'Optional MI/COM review: {code}; original description retained.'
        line.source='Order-only MI/COM selection'
        self.validate_line(line)
        if line.issues: raise ValueError(' '.join(line.issues))

    @staticmethod
    def signature(p):
        return hashlib.sha256(json.dumps({k:p.get(k) for k in ('id','description','uom','color','discontinued')},sort_keys=True).encode()).hexdigest()

    def approve(self,line,product_id,price_mode='piece',feet='0',inches='0',multiplier='1',cost='',reviewer_note='',remember=False):
        if line.kind!='item': raise ValueError('Accounting references and section notes cannot be approved as products.')
        p=self.catalog.get(normal(product_id))
        if not p: raise ValueError('P10 code is not in the catalog. Update catalog.json with a verified item first.')
        f,i,m=number(feet),number(inches),number(multiplier)
        if f<ZERO or f!=f.to_integral_value() or i<ZERO or i>=12 or m<=ZERO:
            raise ValueError('Use whole feet >=0, inches >=0 and <12, and a quantity multiplier >0.')
        if (i*4)!=(i*4).to_integral_value(): raise ValueError('Inches must use 1/4-inch increments.')
        if price_mode not in ('piece','lf'): raise ValueError('Choose piece or lf pricing.')
        if price_mode=='lf' and f+i/12<=ZERO: raise ValueError('Per-LF pricing needs a positive length.')
        if price_mode=='piece' and p['uom'].upper()=='LF' and not clean(reviewer_note):
            raise ValueError('Record why this LF item uses per-piece pricing in the reviewer note.')
        if cost!='' and number(cost)<ZERO: raise ValueError('Cost must be blank or nonnegative.')
        old=(line.feet,line.inches,line.multiplier,line.price_mode)
        line.feet,line.inches,line.multiplier,line.price_mode=f,i,m,price_mode
        if line.target_amount!=money(line.amount):
            line.feet,line.inches,line.multiplier,line.price_mode=old
            raise ValueError('Converted extension does not equal the PDF line amount.')
        conflicts_found=conflicts(line,p)
        if conflicts_found and not clean(reviewer_note):
            line.feet,line.inches,line.multiplier,line.price_mode=old
            raise ValueError('Explain the specification difference before approving: '+' '.join(conflicts_found))
        self.apply_product(line,p)
        line.review_mode=''
        line.cost=decstr(number(cost)) if cost!='' else ''
        line.reviewer_note=clean(reviewer_note)
        line.approved=True;line.issues=[]
        line.basis='User reviewed code, specifications, units, cut length and pricing.'
        line.source='Current review'
        if remember:
            self.overrides[scope_key(line)]={k:decstr(getattr(line,k)) for k in ('product_id','price_mode','feet','inches','multiplier','cost','reviewer_note')}
            self.overrides[scope_key(line)]['catalog_signature']=self.signature(p)
            tmp=self.overrides_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.overrides,indent=2),encoding='utf-8');tmp.replace(self.overrides_path)

    def validate_line(self,line):
        if line.kind!='item': return
        if not line.product_id: return
        if line.multiplier<=ZERO: line.issues.append('Invalid quantity multiplier.')
        if line.feet<ZERO or line.feet!=line.feet.to_integral_value() or line.inches<ZERO or line.inches>=12 or (line.inches*4)!=(line.inches*4).to_integral_value():
            line.issues.append('Invalid cut length: use whole feet and quarter-inch increments below 12 inches.')
        try:
            if line.target_amount!=money(line.amount): line.issues.append('Converted extension differs from source amount.')
        except ValueError as e: line.issues.append(str(e))
        if line.issues: line.approved=False

def validate_export(order):
    problems=list(order.issues)
    if order.pending: problems.append('Unresolved lines: '+', '.join(str(l.n) for l in order.pending))
    target_total=ZERO
    for line in order.lines:
        if line.kind!='item': continue
        if not line.product_id: problems.append(f'Line {line.n}: missing ProductID.'); continue
        try:
            if line.target_amount!=money(line.amount): problems.append(f'Line {line.n}: extension mismatch.')
            target_total+=line.target_amount
        except ValueError as e: problems.append(str(e))
    if order.untaxed is None or money(target_total)!=money(order.untaxed): problems.append('Converted order does not reconcile to source untaxed total.')
    if problems: raise ValueError('\n'.join(dict.fromkeys(problems)))

def p10_rows(order):
    validate_export(order)
    rows=[]
    for l in order.lines:
        if l.kind=='deposit': continue
        if l.kind=='note' and l.review_mode!='mi_com':
            rows.append(['',ZERO,ZERO,ZERO,'',ZERO,'','',l.description]);continue
        comment=f'Odoo {order.order_id} | line {l.n} | '+(f'[{l.code}] ' if l.code else '')+l.description.replace('\n',' / ')
        if l.delivered_quantity is not None and l.delivered_quantity != ZERO:
            comment+=f' | DELIVERED: {decstr(l.delivered_quantity)} {l.uom}; full ordered quantity retained'
        if l.reviewer_note: comment+=' | Review: '+l.reviewer_note
        rows.append([l.product_id,l.feet,l.inches,l.target_quantity,comment,l.target_price,l.cost,l.color,l.description.replace('\n',' / ')])
    return rows

def safe_cell(value):
    # CSV text only: don't let source descriptions execute as Excel formulas.
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@','\t','\r')): return "'"+value
    return decstr(value)

def write_csv(path,headers,rows):
    with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(headers);w.writerows([[safe_cell(x) for x in row] for row in rows])

def export_csv(order,path):
    rows=p10_rows(order)
    if any(str(r[0]).lstrip().startswith(('=','+','-','@')) for r in rows):
        raise ValueError('A ProductID starts with an Excel formula character. Use XLS export to preserve it as literal text.')
    write_csv(path,HEADERS,rows)

def export_xls(order,path):
    from xls_export import xls_bytes
    Path(path).write_bytes(xls_bytes(order))


REVIEW_HEADERS=['Line','Page','Status','Odoo code','Odoo description','Odoo qty','Odoo UOM','Odoo unit price','Source amount','P10 ID','P10 description','P10 UOM','Feet','Inches','Quantity multiplier','Price basis','P10 qty','P10 sales price','Calculated amount','Issues','Mapping basis','Source','Reviewer note','Odoo delivered quantity','Delivery flag','Imported at (UTC)']

def review_rows(order):
    rows=[]
    for l in order.lines:
        try: price,amount=l.target_price,l.target_amount
        except ValueError: price,amount='',''
        if (l.kind!='item' and l.review_mode!='mi_com') or not l.product_id: price,amount='',''
        rows.append([l.n,l.page,l.status,l.code,l.description,l.quantity,l.uom,l.unit_price,l.amount,l.product_id,l.p10_description,l.p10_uom,l.feet,l.inches,l.multiplier,l.price_mode,l.target_quantity,price,amount,' '.join(l.issues),l.basis,l.source,l.reviewer_note,l.delivered_quantity,'Delivered quantities present' if order.has_deliveries else '',order.imported_at])
    return rows

def save_review(order,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    stem=re.sub(r'[^A-Za-z0-9_-]','_',order.order_id)
    write_csv(directory/(stem+'_Review.csv'),REVIEW_HEADERS,review_rows(order))
    payload=asdict(order)
    (directory/(stem+'_Review.json')).write_text(json.dumps(payload,default=decstr,ensure_ascii=False,indent=2),encoding='utf-8')
    e=lambda x:html.escape(decstr(x))
    summary=f'{len([l for l in order.lines if l.kind=="item"])} order items / {len(order.pending)} need review'
    source_label = ('Odoo API snapshot · '+order.imported_at) if order.source_type == 'odoo' else Path(order.source_pdf).name
    delivery_notice = 'Delivered quantities present. Full ordered quantities retained.' if order.has_deliveries else ''
    issues=''.join('<li>'+e(t)+'</li>' for t in order.issues)
    blocks=[]
    for l in order.lines:
        cls='ready' if l.status=='READY' else 'review' if l.status=='REVIEW' else 'note'
        blocks.append(f'<tr class="{cls}"><td>{l.n}<small>Page {l.page}</small></td><td><b>{e(l.code or "Uncoded")}</b><br>{e(l.description)}<small>{e(l.quantity)} {e(l.uom)} × {e(l.unit_price)}</small><small>Delivered: {e(l.delivered_quantity)}</small></td><td>${e(money(l.amount))}</td><td><b>{e(l.product_id or "Choose product")}</b><br>{e(l.p10_description)}<small>{e(l.p10_uom)}; {e(l.feet)} ft {e(l.inches)} in; price/{e(l.price_mode)}</small></td><td><b>{l.status}</b><br>{e(" ".join(l.issues) or l.basis)}</td></tr>')
    output=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(order.order_id)} conversion review</title>
<style>body{{font:15px system-ui,sans-serif;background:#f3f5f7;color:#172331;margin:0}}header{{background:#162d3c;color:white;padding:30px 4vw;border-top:7px solid #b12d35}}h1{{margin:0 0 8px}}main{{padding:22px 4vw}}.metrics{{display:flex;gap:40px;flex-wrap:wrap}}.metrics b{{display:block;font-size:25px}}.notice{{background:#fff5dc;padding:18px;border-left:4px solid #ca9825;margin:20px 0}}table{{width:100%;border-collapse:collapse;background:white}}th{{text-align:left;background:#dfe7eb;padding:12px}}td{{padding:13px;vertical-align:top;border-bottom:1px solid #dde3e7}}td:nth-child(2){{width:29%}}td:nth-child(4){{width:25%}}small{{display:block;color:#52636d;margin-top:7px}}.review td:first-child{{border-left:4px solid #d49a32}}.ready td:first-child{{border-left:4px solid #3c7f6b}}pre{{white-space:pre-wrap}}@media print{{body{{background:white}}header{{color:black;background:white}}main{{padding:0}}tr{{break-inside:avoid}}}}</style>
<header><h1>Odoo {e(order.order_id)} → P10</h1>{e(summary)}</header><main><div class="metrics"><div>Order untaxed<b>${e(order.untaxed)}</b></div><div>Extracted line total<b>${e(money(order.source_sum))}</b></div><div>Tax<b>${e(order.tax)}</b></div></div><pre>{e(order.customer_block)}\nOrder date: {e(order.date)} · Salesperson: {e(order.salesperson)}</pre>
<div class="notice">{e(delivery_notice)} This is a conversion review, not an import file. Resolve every REVIEW row in the app before export. Full ordered quantities are retained. Delivery quantities, when available, are a snapshot at import time. Customer, address, tax, warehouse and payment application are entered or checked on the P10 order header. Cost is blank unless entered in the review. A zero-quantity down-payment reference does not post a payment.</div><ul>{issues}</ul><table><thead><tr><th>Line</th><th>Odoo order line</th><th>Amount</th><th>P10 mapping</th><th>Review</th></tr></thead><tbody>{''.join(blocks)}</tbody></table><small>Source: {e(source_label)} · SHA-256: {e(order.sha256)}</small></main></html>'''
    target=directory/(stem+'_Review.html');target.write_text(output,encoding='utf-8');return target

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pdf',nargs='+',type=Path)
    parser.add_argument('--out',type=Path,default=ROOT/'output')
    parser.add_argument('--data',type=Path,default=ROOT/'seed')
    parser.add_argument('--export',choices=['csv','xls'],help='Export only fully reviewed orders; otherwise review files only.')
    args=parser.parse_args();mapper=Mapper(args.data);failed=False
    for path in args.pdf:
        try:
            order=mapper.map_order(parse_pdf(path))
            folder=args.out/(re.sub(r'[^A-Za-z0-9_-]','_',order.order_id)+'_'+order.sha256[:8])
            report=save_review(order,folder)
            print(f'{order.order_id}: {len(order.lines)} extracted lines; {len(order.pending)} need review; subtotal {order.source_sum}. Review: {report}')
            if args.export:
                target=folder/(order.order_id+'_P10.'+args.export)
                (export_xls if args.export=='xls' else export_csv)(order,target)
                print(f'Created: {target}')
            elif order.issues: failed=True
        except Exception as e: print(f'{path.name}: {e}');failed=True
    return 1 if failed else 0

if __name__=='__main__': raise SystemExit(main())
