import frappe

from frappe.custom.doctype.custom_field.custom_field import create_custom_field


def execute():
	custom_fields = {
		"Purchase Invoice": [
			{
				"fieldname": "pdf_copy",
				"fieldtype": "Attach",
				"label": "Pdf Copy",
				"options": "File",
				"insert_after": "supplier_name",
			},
		],
		"Purchase Credit Note": [
			{
				"fieldname": "pdf_copy",
				"fieldtype": "Attach",
				"label": "Pdf Copy",
				"options": "File",
				"insert_after": "supplier_name",
			},
		],
		"Sales Invoice": [
			{
				"fieldname": "pdf_copy",
				"fieldtype": "Attach",
				"label": "Pdf Copy",
				"options": "File",
				"insert_after": "customer_name",
			},
		],
		"Sales Credit Note": [
			{
				"fieldname": "pdf_copy",
				"fieldtype": "Attach",
				"label": "Pdf Copy",
				"options": "File",
				"insert_after": "customer_name",
			},
		],
		"Supplier Quotation": [
			{
				"fieldname": "pdf_copy",
				"fieldtype": "Attach",
				"label": "Pdf Copy",
				"options": "File",
				"insert_after": "supplier_name",
			},
		],
		"Purchase Receipt": [
			{
				"fieldname": "pdf_copy",
				"fieldtype": "Attach",
				"label": "Pdf Copy",
				"options": "File",
				"insert_after": "supplier_name",
			},
		],
	}

	for doctype, fields in custom_fields.items():
		if frappe.db.exists("DocType", doctype):
			for field in fields:
				create_custom_field(doctype, field, ignore_validate=True)