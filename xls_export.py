"""Native BIFF8 XLS output for Linux; no Excel, macros, or external process."""
from decimal import Decimal
from io import BytesIO
import xlwt

def xls_bytes(order):
    from converter import HEADERS, p10_rows
    rows = p10_rows(order)
    if len(rows) > 65535:
        raise ValueError('This order exceeds the XLS row limit.')
    book = xlwt.Workbook(encoding='utf-8')
    sheet = book.add_sheet('Sheet1')
    header = xlwt.easyxf('font: bold on; pattern: pattern solid, fore_colour gray25;')
    numeric = xlwt.easyxf(num_format_str='0.########')
    for col, title in enumerate(HEADERS):
        sheet.write(0, col, title, header)
        sheet.col(col).width = [24,16,16,16,60,18,18,22,65][col] * 256
    for row_no, row in enumerate(rows, 1):
        for col, value in enumerate(row):
            if value == '' or value is None:
                continue
            if col in (1,2,3,5,6):
                sheet.write(row_no, col, float(Decimal(str(value))), numeric)
            else:
                # xlwt stores strings literally; only explicit Formula objects are formulas.
                text = str(value)
                if len(text) > 32767:
                    raise ValueError(f'Line {row_no}, {HEADERS[col]} exceeds the XLS text limit.')
                sheet.write(row_no, col, text)
    sheet.set_panes_frozen(True)
    sheet.set_horz_split_pos(1)
    out = BytesIO()
    book.save(out)
    return out.getvalue()
