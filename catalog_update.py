"""Read a fresh P10 product export; no spreadsheet authoring dependencies."""
import csv
import json
from pathlib import Path
from converter import clean, normal

def read_rows(path):
    path=Path(path)
    if path.suffix.lower()=='.csv':
        with path.open(encoding='utf-8-sig',newline='') as f: return list(csv.reader(f))
    if path.suffix.lower()=='.xlsx':
        import openpyxl
        w=openpyxl.load_workbook(path,read_only=True,data_only=True)
        rows=list(w.active.values);w.close();return rows
    if path.suffix.lower()=='.xls':
        import xlrd
        w=xlrd.open_workbook(path).sheet_by_index(0)
        return [w.row_values(i) for i in range(w.nrows)]
    raise ValueError('Use a P10 .xls, .xlsx or .csv product export.')

def update_catalog(mapper,path):
    rows=read_rows(path)
    aliases={'STRPRODUCTID':'id','PRODUCT ID':'id','PRODUCTID':'id','MEMDESCRIPTION':'description','DESCRIPTION':'description','STRUNITMEASURE':'uom','UNIT MEASURE':'uom','UOM':'uom','STRCOLOR':'color','COLOR':'color','STRCATEGORY':'category','CATEGORY':'category','YSNALWAYSENABLELM':'linear','YSNDISCONTINUED':'discontinued'}
    header=None
    for n,row in enumerate(rows[:30]):
        h={i:aliases[normal(v)] for i,v in enumerate(row) if normal(v) in aliases}
        if {'id','description','uom'}<=set(h.values()): header=(n,h);break
    if not header: raise ValueError('Expected Product ID, Description and Unit Measure columns in the first 30 rows.')
    n,h=header;found={}
    for row in rows[n+1:]:
        entry={v:clean(row[i]) for i,v in h.items() if i<len(row)}
        if not entry.get('id'): continue
        key=normal(entry['id'])
        if key in found: raise ValueError('Duplicate ProductID in source: '+entry['id'])
        for f in ('linear','discontinued'):
            if f in entry: entry[f]=normal(entry[f]) in ('1','1.0','TRUE','YES')
        prior=mapper.catalog.get(key,{'color':'','category':'','linear':False,'discontinued':False,'hopkinsville':False})
        found[key]={**prior,**entry,'source':Path(path).name}
    if not found: raise ValueError('No product rows found.')
    target=mapper.data_dir/'catalog.json'
    backup=mapper.data_dir/'catalog.before_update.json'
    backup.write_bytes(target.read_bytes())
    # Merge supports subset exports; explicit discontinued flags still block autoapproval.
    mapper.catalog.update(found)
    tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(mapper.catalog,indent=2),encoding='utf-8');tmp.replace(target)
    return len(found)
