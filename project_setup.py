"""Read private product data from a setup ZIP without extracting or executing files."""
import json
from pathlib import Path, PurePosixPath
import shutil
from zipfile import ZipFile, BadZipFile

from converter import normal

FILES=('catalog.json','project_mappings.json','sources.json')
MAX_BYTES=32*1024*1024

def read_setup(stream):
    try:
        with ZipFile(stream) as archive:
            selected={}
            for item in archive.infolist():
                path=PurePosixPath(item.filename)
                if path.name not in FILES or path.parent.name!='seed':continue
                if '..' in path.parts or path.is_absolute():raise ValueError('Invalid setup member path.')
                if path.name in selected:raise ValueError('Duplicate setup file: '+path.name)
                selected[path.name]=item
            if not {'catalog.json','project_mappings.json'}<=selected.keys():
                raise ValueError('ZIP needs seed/catalog.json and seed/project_mappings.json from the private converter package.')
            if sum(item.file_size for item in selected.values())>MAX_BYTES:
                raise ValueError('Expanded setup files exceed 32 MB.')
            result={}
            for name,item in selected.items():
                with archive.open(item) as handle:raw=handle.read(MAX_BYTES+1)
                if len(raw)>MAX_BYTES:raise ValueError('Setup file exceeds 32 MB.')
                result[name]=json.loads(raw.decode('utf-8-sig'))
    except (BadZipFile,UnicodeDecodeError,json.JSONDecodeError) as exc:
        raise ValueError('The ZIP does not contain valid project JSON files.') from exc
    catalog=result['catalog.json'];rules=result['project_mappings.json']
    if not isinstance(catalog,dict) or not 0<len(catalog)<=200000:
        raise ValueError('Catalog must contain 1 to 200,000 products.')
    if not isinstance(rules,dict) or not 0<len(rules)<=100000:
        raise ValueError('Project mappings must contain 1 to 100,000 rules.')
    for key,product in catalog.items():
        if not isinstance(product,dict) or any(not isinstance(product.get(k),str) for k in ('id','description','uom')) or not product['id'].strip():
            raise ValueError('Each catalog item needs a text id, description and uom.')
        if key!=normal(product['id']):raise ValueError('Catalog keys must match normalized product IDs.')
        for field in ('color','category','source'):
            product.setdefault(field,'')
            if not isinstance(product[field],str):raise ValueError('Catalog text field is invalid: '+field)
        for field in ('linear','discontinued','hopkinsville'):
            product.setdefault(field,False)
            if not isinstance(product[field],bool):raise ValueError('Catalog flag must be true or false: '+field)
    for key,rule in rules.items():
        if key!=normal(key) or not isinstance(rule,dict):raise ValueError('Invalid mapping entry.')
        if not isinstance(rule.get('approved'),bool):raise ValueError('Every mapping needs an approved true/false flag.')
        for field in ('product_id','basis','source'):
            if not isinstance(rule.get(field),str):raise ValueError('Mapping text field is invalid: '+field)
        for field in ('source_description','source_uom','p10_uom'):
            if field in rule and not isinstance(rule[field],str):raise ValueError('Invalid mapping unit or description.')
    result.setdefault('sources.json',{})
    if not isinstance(result['sources.json'],dict):raise ValueError('Source metadata must be a JSON object.')
    return result

def install_setup(data,root):
    root=Path(root)
    for name in FILES:
        target=root/name
        if target.exists():shutil.copyfile(target,root/(name+'.before_setup'))
        temporary=root/(name+'.tmp')
        temporary.write_text(json.dumps(data[name],ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(target)
