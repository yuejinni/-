"""
print/print_manager.py — 格口面单打印

触发条件：格口按钮按下（DB201 offset 900+）且格口绿灯（init_num=fj_num!=0）。
技术方案：Jinja2 + pdfkit（替代原 C# Grid++Report/PrintHelper.cs）。
⚠️ 异步调用：print_port_label 含 PDF 生成（3-10s），在 daemon thread 中执行。
"""
import os
import tempfile
import logging

from jinja2 import Template
from core.db import qone, qall

logger = logging.getLogger(__name__)


def print_port_label(db_conn, portno: int, printer_name: str,
                     template_path: str, wkhtmltopdf_path: str):
    """
    格口面单打印：查出该格口所有已落包记录，渲染 HTML 模板，发送到指定打印机。
    ⚠️ JOIN 必须加 batchno，防止历史批次同条码同格口的数据混入。
    """
    import pdfkit
    import win32api

    active_batch = (
        qone(db_conn, "SELECT value FROM sys_config WHERE [key]='active_batchno'") or {}
    ).get("value", "")

    items = qall(db_conn,
        "SELECT se.barcode, sr.goodsno, sr.customer, sr.label_data, "
        "       se.serialnum, se.scanned_at "
        "FROM scan_events se "
        "JOIN sorting_rules sr ON se.barcode = sr.barcode AND se.batchno = sr.batchno "
        "WHERE se.innerport = ? AND se.batchno = ? "
        "ORDER BY se.scanned_at",
        (portno, active_batch))

    if not items:
        logger.info(f"[print] 格口 {portno} 无落包记录，跳过打印")
        return

    port_info = qone(db_conn,
        "SELECT init_num, fj_num FROM sort_ports WHERE portno=?", (portno,))

    # 读取并渲染 HTML 模板
    with open(template_path, encoding='utf-8') as f:
        tmpl = Template(f.read())
    html = tmpl.render(
        portno=portno,
        total=port_info["init_num"] if port_info else len(items),
        items=items,
    )

    # HTML → PDF
    config = pdfkit.configuration(wkhtmltopdf=wkhtmltopdf_path)
    tmp_pdf = os.path.join(tempfile.gettempdir(), f"label_port{portno}.pdf")
    pdfkit.from_string(html, tmp_pdf, configuration=config,
                       options={"page-size": "A4",
                                "margin-top": "5mm",
                                "margin-bottom": "5mm",
                                "encoding": "UTF-8"})

    # 发送到打印机
    win32api.ShellExecute(0, "print", tmp_pdf, f'/d:"{printer_name}"', ".", 0)
    logger.info(f"[print] 格口 {portno} 面单已发送到打印机 {printer_name}")
