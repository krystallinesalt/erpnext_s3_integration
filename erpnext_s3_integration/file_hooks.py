import io
import os
import re

import frappe
from frappe import _
from unidecode import unidecode

ATTACHMENT_PREFIX_BY_DOCTYPE = {
	"Purchase Invoice": "PCHINV",
	"Purchase Credit Note": "PCHCRN",
	"Purchase Order": "PURPQT",
	"Sales Invoice": "INVETR",
	"Sales Credit Note": "RINETR",
}

MAX_ATTACHMENT_SIZE_BYTES = 200 * 1024 * 1024

PDF_COPY_FIELDNAME = "pdf_copy"

PDF_COPY_ATTACHMENT_DOCTYPES = (
	"Purchase Invoice",
	"Purchase Credit Note",
	"Purchase Order",
	"Purchase Receipt",
	"Sales Invoice",
	"Sales Credit Note",
	"Supplier Quotation",
)


def _slugify(value):
	if not value:
		return ""
	value = unidecode(str(value)).strip()
	value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
	return value or "unnamed"


def _get_attachment_subfolder(settings):
	subfolder = getattr(settings, "attachment_subfolder", None) or settings.get("attachment_subfolder") or "attachments"
	subfolder = str(subfolder).strip().strip("/")
	return subfolder or "attachments"


def _get_s3_settings():
	frappe.clear_cache(doctype="S3 Integration Settings")
	return frappe.get_doc("S3 Integration Settings", "S3 Integration Settings")


def _sync_attachment_tracking(file_doc):
	"""Create/refresh the S3 PDF Attachment tracking row and keep the parent's pdf_copy
	pointer in sync. This is the single place that owns pdf_copy going forward — it is
	status-aware (won't resurrect an attachment the user has soft-deleted), unlike a plain
	unconditional field write would be."""
	from erpnext_s3_integration.attachment_tracking import sync_after_upload

	sync_after_upload(file_doc)


RETURN_PREFIX_BY_DOCTYPE = {
	"Sales Invoice": ATTACHMENT_PREFIX_BY_DOCTYPE["Sales Credit Note"],
	"Purchase Invoice": ATTACHMENT_PREFIX_BY_DOCTYPE["Purchase Credit Note"],
}


def _get_attachment_prefix(attached_to_doctype, attached_to_name):
	"""ERPNext has no separate credit-note doctypes: a credit note is a Sales or
	Purchase Invoice saved with the 'Is Return' checkbox, so the doctype alone
	can't distinguish the two - the parent's is_return flag has to be consulted."""
	return_prefix = RETURN_PREFIX_BY_DOCTYPE.get(attached_to_doctype)
	if (
		return_prefix
		and attached_to_name
		and frappe.db.get_value(attached_to_doctype, attached_to_name, "is_return")
	):
		return return_prefix
	return ATTACHMENT_PREFIX_BY_DOCTYPE.get(attached_to_doctype)


def _base_attachment_name(attached_to_doctype, attached_to_name):
	base_name = _slugify(attached_to_name)
	prefix = _get_attachment_prefix(attached_to_doctype, attached_to_name)
	if prefix:
		base_name = f"{prefix}-{base_name}"
	return base_name


def _matches_finalized_base_name(file_name, base_name):
	"""Whether file_name already looks like <base_name>.pdf or <base_name>-<n>.pdf."""
	current_base, _ext = os.path.splitext((file_name or "").strip())
	if not current_base:
		return False
	return current_base == base_name or current_base.startswith(f"{base_name}-")


def build_attachment_name(file_doc):
	"""Build a deterministic attachment name using the parent document name and versioning."""
	attached_to_name = getattr(file_doc, "attached_to_name", None)
	attached_to_doctype = getattr(file_doc, "attached_to_doctype", None)
	if attached_to_name:
		prefix = _get_attachment_prefix(attached_to_doctype, attached_to_name)
		base_name = _base_attachment_name(attached_to_doctype, attached_to_name)
		ext = os.path.splitext(getattr(file_doc, "file_name", "") or "")[1] or ".pdf"
		if not ext:
			ext = ".pdf"
		if ext.lower() != ".pdf":
			ext = ".pdf"

		version = 0
		if attached_to_doctype and attached_to_name:
			slugged_name = _slugify(attached_to_name)
			existing_files = frappe.get_all(
				"File",
				filters={
					"attached_to_doctype": file_doc.attached_to_doctype,
					"attached_to_name": attached_to_name,
				},
				fields=["file_name"],
				limit=1000,
			)
			for row in existing_files:
				existing_name = (row.get("file_name") or "").strip()
				if not existing_name:
					continue
				existing_base, _ = os.path.splitext(existing_name)
				if not existing_base:
					continue
				if existing_base == base_name or existing_base.startswith(f"{base_name}-"):
					version += 1
				elif prefix and existing_base == slugged_name:
					version += 1
				elif prefix and existing_base.startswith(f"{prefix}-{slugged_name}-"):
					version += 1
				elif existing_base.startswith(f"{slugged_name}-"):
					version += 1

		if version:
			return f"{base_name}-{version}{ext}"
		return f"{base_name}{ext}"

	default_name = getattr(file_doc, "file_name", None) or "attachment.pdf"
	if not default_name.lower().endswith(".pdf"):
		default_name = f"{os.path.splitext(default_name)[0]}.pdf"
	return default_name


def rename_attachment_to_final_name(file_doc, settings=None):
	"""Rename an S3-backed attachment once the parent document has a real final name."""
	if not getattr(file_doc, "file_url", None) or not file_doc.file_url.startswith("/s3/"):
		return False

	if settings is None:
		settings = _get_s3_settings()
	if not getattr(settings, "enable_attachments_s3", False):
		return False

	# Once a file already carries its deterministic final name, leave it alone for good.
	# build_attachment_name's version suffix is based on a live count of sibling attachments,
	# which naturally shifts as more files are added/removed later - recomputing it on every
	# subsequent save (rename_attached_files_for_parent reprocesses every attached file on
	# every parent save, not just the newly uploaded one) would keep moving an already-settled
	# file to a new S3 key for no reason, and risks losing it if a copy/delete step ever fails
	# partway.
	attached_to_doctype = getattr(file_doc, "attached_to_doctype", None)
	attached_to_name = getattr(file_doc, "attached_to_name", None)
	if getattr(file_doc, "name", None) and attached_to_name:
		base_name = _base_attachment_name(attached_to_doctype, attached_to_name)
		if _matches_finalized_base_name(getattr(file_doc, "file_name", None), base_name):
			_sync_attachment_tracking(file_doc)
			return False

	target_name = build_attachment_name(file_doc)
	target_key = generate_s3_key(file_doc, settings)
	current_key = file_doc.file_url.replace("/s3/", "", 1)
	current_name = getattr(file_doc, "file_name", None) or ""

	if current_name == target_name and current_key == target_key:
		_sync_attachment_tracking(file_doc)
		return False

	from erpnext_s3_integration.s3_client import S3Client

	s3_client = S3Client()
	s3_client.move_object(current_key, target_key, is_public=not file_doc.is_private)

	# move_object is an immediate, irreversible external side effect (S3 copy + delete) that
	# is not part of the surrounding DB transaction. If anything later in this request fails
	# and the transaction rolls back, the DB would revert to pointing at a key that no longer
	# exists on S3 while the real object now sits under target_key - a dangling reference with
	# no code path back to it. Commit immediately so the DB is never out of sync with the S3
	# side effect that has already, unconditionally, happened.
	file_doc.file_name = target_name
	file_doc.file_url = f"/s3/{target_key}"
	if getattr(file_doc, "name", None):
		file_doc.db_set("file_name", target_name)
		file_doc.db_set("file_url", file_doc.file_url)
	_sync_attachment_tracking(file_doc)
	if not frappe.flags.in_test:
		frappe.db.commit()
	return True


def rename_attached_files_for_parent(doc, method=None):
	"""Rename any S3-backed attachments linked to a parent document after the parent gets its final name."""
	if not getattr(doc, "name", None) or str(doc.name).startswith("new-"):
		return

	settings = _get_s3_settings()
	if not getattr(settings, "enable_attachments_s3", False):
		return

	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": doc.doctype, "attached_to_name": doc.name},
		fields=["name"],
		limit_page_length=1000,
	)
	for row in files:
		file_doc = frappe.get_doc("File", row.name)
		rename_attachment_to_final_name(file_doc, settings=settings)


def validate_file_upload(file_doc):
	"""Ensure only PDF attachments within the size limit are allowed through the S3 upload path."""
	filename = (getattr(file_doc, "file_name", "") or "").lower()
	mime_type = (getattr(file_doc, "mime_type", "") or "").lower()
	content_type = (getattr(file_doc, "content_type", "") or "").lower()
	content = getattr(file_doc, "content", None)

	if isinstance(content, (bytes, bytearray)):
		size_bytes = len(content)
	elif isinstance(content, str):
		size_bytes = len(content.encode("utf-8"))
	else:
		size_bytes = 0

	if size_bytes > MAX_ATTACHMENT_SIZE_BYTES:
		frappe.throw(
			_(
				"Uploaded file exceeds the 200 MB limit for S3 attachments. Please reduce the file size and try again."
			)
		)

	if filename.endswith(".pdf") or mime_type in {"application/pdf", "application/x-pdf"} or content_type in {"application/pdf", "application/x-pdf"}:
		return

	if isinstance(content, (bytes, bytearray)) and content.startswith(b"%PDF"):
		return
	if isinstance(content, str) and content.startswith("%PDF"):
		return

	frappe.throw(_("Only PDF files are allowed for uploads. Please convert the file to PDF and try again."))


def generate_s3_key(file_doc, settings):
	"""Generates a deterministic S3 key mirroring native Frappe paths."""
	folder_prefix = settings.get("folder_prefix") or ""
	if folder_prefix and not folder_prefix.endswith("/"):
		folder_prefix += "/"

	# If this is an existing file being migrated, its file_url will simply be a local path like /files/x.png
	file_url = getattr(file_doc, "file_url", None)
	if file_url and file_url.startswith("/") and not file_url.startswith("/s3/"):
		base_path = file_url.lstrip("/")
	else:
		attachment_name = build_attachment_name(file_doc)
		scope = "private" if getattr(file_doc, "is_private", False) else "public"
		attachment_subfolder = _get_attachment_subfolder(settings)
		path_parts = [attachment_subfolder, scope]

		attached_to_doctype = getattr(file_doc, "attached_to_doctype", None)
		attached_to_name = getattr(file_doc, "attached_to_name", None)
		if attached_to_doctype and attached_to_name:
			path_parts.extend([_slugify(attached_to_doctype), _slugify(attached_to_name)])
		else:
			filename = unidecode(getattr(file_doc, "file_name", "") or "").replace(" ", "_")
			identifier = (
				f"{file_doc.content_hash}-{filename}" if getattr(file_doc, "content_hash", None) else filename
			)
			path_parts.append(identifier)
		path_parts.append(attachment_name)
		base_path = "/".join(path_parts)

	return f"{folder_prefix}{base_path}"


def before_insert(file_doc, method):
	"""Intercept file insertion to upload to S3."""
	if getattr(getattr(file_doc, "flags", None), "s3_before_insert_run", False):
		return True

	if getattr(file_doc, "file_url", None) and file_doc.file_url.startswith("/s3/"):
		file_doc.flags.s3_before_insert_run = True
		return True

	settings = _get_s3_settings()
	if not settings.enable_attachments_s3:
		return False

	# Only intercept if it's a new upload with content
	if (
		hasattr(file_doc, "is_file_path") and file_doc.is_file_path() and not frappe.flags.in_test
	) or getattr(file_doc, "is_folder", False):
		return False

	validate_file_upload(file_doc)

	# If no content was provided during insert but they uploaded a file, Frappe triggers `save_file`
	# Which writes to disk. We need to handle this by checking if the content exists.
	try:
		content = file_doc.get_content()
	except (FileNotFoundError, OSError):
		file_doc.flags.s3_before_insert_run = True
		return True
	if not content and not frappe.flags.in_test:
		return False

	attached_to_field = (getattr(file_doc, "attached_to_field", None) or "").strip()
	if attached_to_field == PDF_COPY_FIELDNAME:
		from erpnext_s3_integration.attachment_tracking import reject_if_duplicate_pdf_attachment

		reject_if_duplicate_pdf_attachment(file_doc, content)

	from erpnext_s3_integration.s3_client import S3Client

	file_doc.flags.s3_before_insert_run = True

	try:
		s3_client = S3Client()

		# Generate the predictable S3 key
		s3_key = generate_s3_key(file_doc, settings)
		attachment_name = build_attachment_name(file_doc)
		file_doc.file_name = attachment_name

		# Upload the file
		is_public = not file_doc.is_private
		content_stream = io.BytesIO(content) if isinstance(content, bytes) else io.BytesIO(content.encode())
		s3_client.upload_fileobj(content_stream, s3_key, file_doc.get("mime_type"), is_public)

		file_doc.file_url = f"/s3/{s3_key}"
		file_doc.content = None

	except Exception as e:
		frappe.throw(f"Error uploading file to S3: {e}")

	return True


def on_trash(file_doc, method):
	"""Handle deletion from S3."""
	if frappe.db.exists("S3 PDF Attachment", {"file": file_doc.name}) and "System Manager" not in frappe.get_roles():
		frappe.throw(
			_(
				"This file is tracked as a PDF attachment. Please use the Delete action in the "
				"document's Attachments card instead of removing it from here."
			)
		)

	settings = _get_s3_settings()
	if not settings.enable_attachments_s3 or not settings.delete_from_s3_on_file_delete:
		return

	if not file_doc.file_url or not file_doc.file_url.startswith("/s3/"):
		return

	s3_key = file_doc.file_url.replace("/s3/", "", 1)

	from erpnext_s3_integration.s3_client import S3Client

	s3_client = S3Client()
	s3_client.delete_object(s3_key)
