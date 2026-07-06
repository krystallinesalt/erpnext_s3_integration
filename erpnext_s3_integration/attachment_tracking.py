import frappe
from frappe import _
from frappe.utils.file_manager import get_content_hash

from erpnext_s3_integration.file_hooks import PDF_COPY_ATTACHMENT_DOCTYPES, PDF_COPY_FIELDNAME


def _validate_supported_doctype(doctype):
	if doctype not in PDF_COPY_ATTACHMENT_DOCTYPES:
		frappe.throw(_("{0} does not support PDF attachment tracking.").format(doctype))


def _lock_parent(doctype, docname):
	"""Serialize concurrent attach/delete/restore calls against the same parent document."""
	frappe.db.get_value(doctype, docname, "name", for_update=True)


def reject_if_duplicate_pdf_attachment(file_doc, content):
	"""Abort the upload if identical content is already tracked (active or deleted) for this parent.

	Runs from before_insert, ahead of the S3 upload, so a rejected duplicate never creates
	an S3 object or a File row. The computed hash is stashed on file_doc.content_hash (a
	native File field) so sync_after_upload can pick it up later without recomputing it.
	"""
	attached_to_doctype = getattr(file_doc, "attached_to_doctype", None)
	attached_to_name = getattr(file_doc, "attached_to_name", None)
	if not attached_to_doctype or not attached_to_name or str(attached_to_name).startswith("new-"):
		return

	content_hash = get_content_hash(content)

	existing = frappe.db.get_value(
		"S3 PDF Attachment",
		{
			"attached_to_doctype": attached_to_doctype,
			"attached_to_name": attached_to_name,
			"content_hash": content_hash,
		},
		["file_name", "status"],
		as_dict=True,
	)
	if existing:
		frappe.throw(
			_("An identical PDF ({0}) is already attached to this document ({1}).").format(
				existing.file_name, existing.status.lower()
			)
		)

	file_doc.content_hash = content_hash


def sync_after_upload(file_doc):
	"""Create/refresh the tracking row for a (re)named PDF attachment and mark it Active,
	retiring whatever was previously Active for the same parent document.

	`rename_attached_files_for_parent` reprocesses *every* File historically attached to a
	parent on *every* save of that parent (not just the file that was just uploaded), so this
	function runs repeatedly for old, already-tracked files too. If one of those is already
	Deleted, a routine unrelated save of the parent must not silently resurrect it back to
	Active - only refresh its denormalized fields and leave its status alone.
	"""
	attached_to_field = (getattr(file_doc, "attached_to_field", None) or "").strip()
	if attached_to_field != PDF_COPY_FIELDNAME:
		return

	attached_to_doctype = getattr(file_doc, "attached_to_doctype", None)
	attached_to_name = getattr(file_doc, "attached_to_name", None)
	if not attached_to_doctype or not attached_to_name or str(attached_to_name).startswith("new-"):
		return

	values = {
		"attached_to_doctype": attached_to_doctype,
		"attached_to_name": attached_to_name,
		"file": file_doc.name,
		"file_name": file_doc.file_name,
		"file_url": file_doc.file_url,
	}
	content_hash = getattr(file_doc, "content_hash", None)
	if content_hash:
		values["content_hash"] = content_hash

	tracking = frappe.db.get_value(
		"S3 PDF Attachment", {"file": file_doc.name}, ["name", "status"], as_dict=True
	)
	if tracking:
		frappe.db.set_value("S3 PDF Attachment", tracking.name, values)
		if tracking.status == "Deleted":
			return
		tracking_name = tracking.name
	else:
		tracking_name = (
			frappe.get_doc({"doctype": "S3 PDF Attachment", "status": "Active", **values})
			.insert(ignore_permissions=True)
			.name
		)

	_activate(tracking_name, attached_to_doctype, attached_to_name)


def _activate(tracking_name, parent_doctype, parent_name):
	"""Make tracking_name the sole Active attachment for parent_doctype/parent_name."""
	frappe.db.set_value(
		"S3 PDF Attachment",
		{
			"attached_to_doctype": parent_doctype,
			"attached_to_name": parent_name,
			"status": "Active",
			"name": ["!=", tracking_name],
		},
		"status",
		"Deleted",
	)
	frappe.db.set_value("S3 PDF Attachment", tracking_name, "status", "Active")
	file_url = frappe.db.get_value("S3 PDF Attachment", tracking_name, "file_url")
	frappe.db.set_value(parent_doctype, parent_name, PDF_COPY_FIELDNAME, file_url, update_modified=False)


@frappe.whitelist()
def get_attachment_history(doctype, docname):
	_validate_supported_doctype(doctype)
	frappe.has_permission(doctype, "read", doc=docname, throw=True)

	rows = frappe.get_all(
		"S3 PDF Attachment",
		filters={"attached_to_doctype": doctype, "attached_to_name": docname},
		fields=["name", "file", "file_name", "file_url", "status", "creation", "modified"],
		order_by="creation desc",
	)

	active = None
	deleted = []
	for row in rows:
		if not frappe.db.exists("File", row.file):
			# Underlying File was removed by some other path; don't offer a dead link.
			continue
		if row.status == "Active" and not active:
			active = row
		else:
			deleted.append(row)

	return {"active": active, "deleted": deleted}


@frappe.whitelist()
def finalize_pdf_attachment(doctype, docname, file):
	"""Immediately rename/activate a freshly uploaded attachment on an already-saved document,
	without waiting for the user to save the whole form."""
	_validate_supported_doctype(doctype)
	frappe.has_permission(doctype, "write", doc=docname, throw=True)
	_lock_parent(doctype, docname)

	file_doc = frappe.get_doc("File", file)
	if file_doc.attached_to_doctype != doctype or file_doc.attached_to_name != docname:
		frappe.throw(_("File does not belong to this document."))

	from erpnext_s3_integration.file_hooks import _get_s3_settings, rename_attachment_to_final_name

	rename_attachment_to_final_name(file_doc, settings=_get_s3_settings())

	return get_attachment_history(doctype, docname)


@frappe.whitelist()
def soft_delete_pdf_attachment(name):
	tracking = frappe.get_doc("S3 PDF Attachment", name)
	_validate_supported_doctype(tracking.attached_to_doctype)
	frappe.has_permission(tracking.attached_to_doctype, "write", doc=tracking.attached_to_name, throw=True)
	_lock_parent(tracking.attached_to_doctype, tracking.attached_to_name)

	if tracking.status != "Deleted":
		frappe.db.set_value("S3 PDF Attachment", name, "status", "Deleted")
		frappe.db.set_value(
			tracking.attached_to_doctype,
			tracking.attached_to_name,
			PDF_COPY_FIELDNAME,
			None,
			update_modified=False,
		)

	return get_attachment_history(tracking.attached_to_doctype, tracking.attached_to_name)


@frappe.whitelist()
def restore_pdf_attachment(name):
	tracking = frappe.get_doc("S3 PDF Attachment", name)
	_validate_supported_doctype(tracking.attached_to_doctype)
	frappe.has_permission(tracking.attached_to_doctype, "write", doc=tracking.attached_to_name, throw=True)
	_lock_parent(tracking.attached_to_doctype, tracking.attached_to_name)

	if not frappe.db.exists("File", tracking.file):
		frappe.throw(_("The underlying file no longer exists and cannot be restored."))

	_activate(name, tracking.attached_to_doctype, tracking.attached_to_name)

	return get_attachment_history(tracking.attached_to_doctype, tracking.attached_to_name)
