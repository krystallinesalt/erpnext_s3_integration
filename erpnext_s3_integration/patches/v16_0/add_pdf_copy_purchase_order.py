import frappe

from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def execute():
    doctype = "Purchase Order"
    if frappe.db.exists("DocType", doctype):
        field = {
            "fieldname": "pdf_copy",
            "fieldtype": "Attach",
            "label": "Pdf Copy",
            "options": "File",
            "insert_after": "supplier_name",
        }
        create_custom_field(doctype, field, ignore_validate=True)
