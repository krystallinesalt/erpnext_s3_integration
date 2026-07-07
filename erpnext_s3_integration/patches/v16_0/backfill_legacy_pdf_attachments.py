import frappe
from frappe.utils.file_manager import get_content_hash

from erpnext_s3_integration.file_hooks import PDF_COPY_ATTACHMENT_DOCTYPES, PDF_COPY_FIELDNAME


def get_untracked_files():
	"""S3-backed files attached to supported doctypes that have no tracking row.

	backfill_pdf_attachment_history only covered files uploaded through the Attachments
	card (attached_to_field == pdf_copy). Files attached before that card existed - via
	the native sidebar or since-removed custom fields - carry a NULL/other fieldname, so
	the tracking sync skips them forever and they never show up in the card. The /s3/
	prefix restriction keeps plain local uploads (which never went through this app's
	PDF-only pipeline) out of the card.
	"""
	file = frappe.qb.DocType("File")
	tracking = frappe.qb.DocType("S3 PDF Attachment")
	return (
		frappe.qb.from_(file)
		.left_join(tracking)
		.on(tracking.file == file.name)
		.select(
			file.name,
			file.file_name,
			file.file_url,
			file.attached_to_doctype,
			file.attached_to_name,
			file.attached_to_field,
			file.content_hash,
			file.creation,
		)
		.where(tracking.name.isnull())
		.where(file.is_folder == 0)
		.where(file.attached_to_doctype.isin(list(PDF_COPY_ATTACHMENT_DOCTYPES)))
		.where(file.attached_to_name.notnull())
		.where(file.file_url.like("/s3/%"))
		.orderby(file.creation)
		.run(as_dict=True)
	)


def execute():
	if get_untracked_files():
		frappe.enqueue(
			"erpnext_s3_integration.patches.v16_0.backfill_legacy_pdf_attachments.run_backfill",
			queue="long",
			timeout=3600,
		)


def run_backfill():
	from erpnext_s3_integration.attachment_tracking import _activate
	from erpnext_s3_integration.file_hooks import (
		_get_s3_settings,
		rename_attachment_to_final_name,
	)

	newest_by_parent = {}

	for f in get_untracked_files():
		try:
			if str(f.attached_to_name).startswith("new-") or not frappe.db.exists(
				f.attached_to_doctype, f.attached_to_name
			):
				# Attached to a draft that was never saved; nothing to backfill against.
				continue

			if (f.attached_to_field or "").strip() != PDF_COPY_FIELDNAME:
				frappe.db.set_value(
					"File", f.name, "attached_to_field", PDF_COPY_FIELDNAME, update_modified=False
				)

			content_hash = f.content_hash
			if not content_hash:
				try:
					content_hash = get_content_hash(frappe.get_doc("File", f.name).get_content())
				except Exception:
					frappe.log_error(
						message=frappe.get_traceback(),
						title=f"S3 PDF Attachment legacy backfill: could not hash {f.name}",
					)
					content_hash = None

			# Everything starts as Deleted (restorable history); the newest file per parent
			# is promoted afterwards so exactly one attachment ends up Active.
			tracking_name = (
				frappe.get_doc(
					{
						"doctype": "S3 PDF Attachment",
						"attached_to_doctype": f.attached_to_doctype,
						"attached_to_name": f.attached_to_name,
						"file": f.name,
						"file_name": f.file_name,
						"file_url": f.file_url,
						"content_hash": content_hash,
						"status": "Deleted",
					}
				)
				.insert(ignore_permissions=True)
				.name
			)

			# get_untracked_files is ordered by creation, so the last write wins.
			newest_by_parent[(f.attached_to_doctype, f.attached_to_name)] = (tracking_name, f.name)
		except Exception:
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"S3 PDF Attachment legacy backfill failed for File {f.name}",
			)

	settings = _get_s3_settings()

	for (doctype, docname), (tracking_name, file_name) in newest_by_parent.items():
		try:
			if not frappe.db.exists(
				"S3 PDF Attachment",
				{"attached_to_doctype": doctype, "attached_to_name": docname, "status": "Active"},
			):
				_activate(tracking_name, doctype, docname)

			# Legacy files may still carry pre-rename names (e.g. from a draft); give every
			# file of the parent its deterministic final name now instead of waiting for the
			# next incidental save. Runs after status assignment because the rename path's
			# tracking sync respects (and must find) the Deleted/Active split decided above.
			for row in frappe.get_all(
				"File",
				filters={"attached_to_doctype": doctype, "attached_to_name": docname},
				pluck="name",
			):
				rename_attachment_to_final_name(frappe.get_doc("File", row), settings=settings)
		except Exception:
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"S3 PDF Attachment legacy backfill: finalize failed for {doctype} {docname}",
			)

	frappe.db.commit()
