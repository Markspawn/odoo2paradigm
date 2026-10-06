"""Fictitious products and rules for automated tests; never deploy as P10 setup."""
import json
from pathlib import Path

def write_seed(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    def item(code,description,uom='EA',color='',category=''):
        return {'id':code,'description':description,'uom':uom,'color':color,'category':category,'linear':uom=='LF','discontinued':False,'source':'Synthetic test fixture','hopkinsville':False}
    rows=[
        item('DEMOPOST','6 x 6 x 18 Pressure Treated Post'),
        item('T9ZZ','29 Ga. Galvalume Tuff Rib','LF','Galvalume','Panels'),
        item('DEMOCREDIT','Test discount'),item('DEMOADJ2','Other test adjustment'),
        item('DEMOCLASH','Ridge Cap 29 Ga. Galvanized (10\' 9")','EA','G-90 Galvanized','Trim'),
        item('DEMOTRIM','Ridge Cap 29 Ga. Galvalume (10\' 9")','EA','Galvalume','Trim'),
        item('DEMOBAG','1.5" Galvalume Wood Binder Screws, Bag of 250','EA','Galvalume'),
        item('DEMOFOREST','1.5" Forest Screws, Bag of 250','EA','Forest'),
    ]
    catalog={item['id']:item for item in rows}
    rules={'DEMOSCREW':{'product_id':'DEMOBAG','approved':True,'basis':'Synthetic package mapping','source':'Synthetic test fixture','source_uom':'Bag of 250'}}
    for name,data in [('catalog.json',catalog),('project_mappings.json',rules),('sources.json',{'note':'Fictitious test data'})]:
        (directory/name).write_text(json.dumps(data),encoding='utf-8')
    return directory
