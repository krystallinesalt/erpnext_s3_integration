import frappe
from frappe.utils.file_manager import get_content_hash

from erpnext_s3_integration.file_hooks import PDF_COPY_ATTACHMENT_DOCTYPES, PDF_COPY_FIELDNAME


def execute():
	files = frappe.get_all(
		"File",
		filters={
			"attached_to_field": PDF_COPY_FIELDNAME,
			"attached_to_doctype": ["in", PDF_COPY_ATTACHMENT_DOCTYPES],
			"is_folder": 0,
		},
		fields=["name", "file_name", "file_url", "attached_to_doctype", "attached_to_name", "content_hash"],
	)

	if not files:
		return

	frappe.enqueue(
		"erpnext_s3_integration.patches.v16_0.backfill_pdf_attachment_history.run_backfill",
		queue="long",
		timeout=3600,
	)


def run_backfill():
	files = frappe.get_all(
		"File",
		filters={
			"attached_to_field": PDF_COPY_FIELDNAME,
			"attached_to_doctype": ["in", PDF_COPY_ATTACHMENT_DOCTYPES],
			"is_folder": 0,
		},
		fields=["name", "file_name", "file_url", "attached_to_doctype", "attached_to_name", "content_hash"],
	)

	for f in files:
		try:
			if frappe.db.exists("S3 PDF Attachment", {"file": f.name}):
				continue
			if not frappe.db.exists(f.attached_to_doctype, f.attached_to_name):
				continue

			current_pdf_copy = frappe.db.get_value(
				f.attached_to_doctype, f.attached_to_name, PDF_COPY_FIELDNAME
			)
			status = "Active" if current_pdf_copy and current_pdf_copy == f.file_url else "Deleted"

			content_hash = f.content_hash
			if not content_hash:
				try:
					file_doc = frappe.get_doc("File", f.name)
					content_hash = get_content_hash(file_doc.get_content())
				except Exception:
					frappe.log_error(
						message=frappe.get_traceback(),
						title=f"S3 PDF Attachment backfill: could not hash {f.name}",
					)
					content_hash = None

			frappe.get_doc(
				{
					"doctype": "S3 PDF Attachment",
					"attached_to_doctype": f.attached_to_doctype,
					"attached_to_name": f.attached_to_name,
					"file": f.name,
					"file_name": f.file_name,
					"file_url": f.file_url,
					"content_hash": content_hash,
					"status": status,
				}
			).insert(ignore_permissions=True)
		except Exception:
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"S3 PDF Attachment backfill failed for File {f.name}",
			)

	frappe.db.commit()
