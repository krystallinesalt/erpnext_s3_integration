import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

from erpnext_s3_integration.file_hooks import PDF_COPY_ATTACHMENT_DOCTYPES, PDF_COPY_FIELDNAME


def execute():
	for doctype in PDF_COPY_ATTACHMENT_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue

		pdf_copy_field = frappe.db.get_value(
			"Custom Field", {"dt": doctype, "fieldname": PDF_COPY_FIELDNAME}
		)
		if pdf_copy_field:
			frappe.db.set_value(
				"Custom Field",
				pdf_copy_field,
				{
					"hidden": 1,
					"read_only": 1,
					"in_list_view": 0,
					"in_standard_filter": 0,
					"allow_on_submit": 1,
				},
				update_modified=False,
			)

		create_custom_field(
			doctype,
			{
				"fieldname": "attachments_section",
				"fieldtype": "Section Break",
				"label": "Attachments",
				"insert_after": PDF_COPY_FIELDNAME,
			},
			ignore_validate=True,
		)
		create_custom_field(
			doctype,
			{
				"fieldname": "attachments_card_html",
				"fieldtype": "HTML",
				"label": "Attachments Card",
				"insert_after": "attachments_section",
			},
			ignore_validate=True,
		)

	frappe.clear_cache()
