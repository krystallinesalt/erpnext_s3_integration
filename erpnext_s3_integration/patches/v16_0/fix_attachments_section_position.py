import frappe

from erpnext_s3_integration.file_hooks import PDF_COPY_ATTACHMENT_DOCTYPES, PDF_COPY_FIELDNAME

CARD_FIELDNAMES = {PDF_COPY_FIELDNAME, "attachments_section", "attachments_card_html"}


def execute():
	"""Move the Attachments section below the first form section.

	The section break was anchored to pdf_copy (right after the party name
	field), which split the first section and rendered the attachments card
	over company/status fields.
	"""
	for doctype in PDF_COPY_ATTACHMENT_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue

		section_field = frappe.db.get_value(
			"Custom Field", {"dt": doctype, "fieldname": "attachments_section"}
		)
		if not section_field:
			continue

		anchor = _get_first_section_end(doctype)
		if anchor:
			frappe.db.set_value(
				"Custom Field", section_field, "insert_after", anchor, update_modified=False
			)

	frappe.clear_cache()


def _get_first_section_end(doctype):
	pdf_copy_anchor = frappe.db.get_value(
		"Custom Field", {"dt": doctype, "fieldname": PDF_COPY_FIELDNAME}, "insert_after"
	)
	if not pdf_copy_anchor:
		return None

	fields = [
		df for df in frappe.get_meta(doctype).fields if df.fieldname not in CARD_FIELDNAMES
	]
	start = next((i for i, df in enumerate(fields) if df.fieldname == pdf_copy_anchor), None)
	if start is None:
		return None

	anchor = None
	for df in fields[start:]:
		if df.fieldtype in ("Section Break", "Tab Break"):
			break
		anchor = df.fieldname
	return anchor
