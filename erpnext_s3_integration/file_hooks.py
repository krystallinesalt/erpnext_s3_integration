import io
import os
import re

import frappe
from frappe import _
from unidecode import unidecode

ATTACHMENT_PREFIX_BY_DOCTYPE = {
	"Purchase Invoice": "PCHINV",
	"Purchase Credit Note": "PCHCRN",
	"Sales Invoice": "INVETR",
	"Sales Credit Note": "RINETR",
}

MAX_ATTACHMENT_SIZE_BYTES = 200 * 1024 * 1024


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


def build_attachment_name(file_doc):
	"""Build a deterministic attachment name using the parent document name and versioning."""
	attached_to_name = getattr(file_doc, "attached_to_name", None)
	attached_to_doctype = getattr(file_doc, "attached_to_doctype", None)
	if attached_to_name:
		base_name = _slugify(attached_to_name)
		prefix = ATTACHMENT_PREFIX_BY_DOCTYPE.get(attached_to_doctype)
		if prefix:
			base_name = f"{prefix}-{base_name}"
		ext = os.path.splitext(getattr(file_doc, "file_name", "") or "")[1] or ".pdf"
		if not ext:
			ext = ".pdf"
		if ext.lower() != ".pdf":
			ext = ".pdf"

		version = 0
		if attached_to_doctype and attached_to_name:
			existing_files = frappe.get_all(
				"File",
				filters={
					"attached_to_doctype": file_doc.attached_to_doctype,
					"attached_to_name": attached_to_name,
				},
				fields=["file_name"],
				limit_page_length=1000,
			)
			for row in existing_files:
				existing_name = (row.get("file_name") or "").strip()
				if not existing_name:
					continue
				existing_base, _ = os.path.splitext(existing_name)
				if existing_base == base_name or existing_base.startswith(f"{base_name}-"):
					version += 1

		if version:
			return f"{base_name}-{version}{ext}"
		return f"{base_name}{ext}"

	default_name = getattr(file_doc, "file_name", None) or "attachment.pdf"
	if not default_name.lower().endswith(".pdf"):
		default_name = f"{os.path.splitext(default_name)[0]}.pdf"
	return default_name


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
	settings = frappe.get_single("S3 Integration Settings")
	if not settings.enable_attachments_s3:
		return

	# Only intercept if it's a new upload with content
	if (
		hasattr(file_doc, "is_file_path") and file_doc.is_file_path() and not frappe.flags.in_test
	) or getattr(file_doc, "is_folder", False):
		return

	validate_file_upload(file_doc)

	# If no content was provided during insert but they uploaded a file, Frappe triggers `save_file`
	# Which writes to disk. We need to handle this by checking if the content exists.
	content = file_doc.get_content()
	if not content and not frappe.flags.in_test:
		return

	from erpnext_s3_integration.s3_client import S3Client

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


def on_trash(file_doc, method):
	"""Handle deletion from S3."""
	settings = frappe.get_single("S3 Integration Settings")
	if not settings.enable_attachments_s3 or not settings.delete_from_s3_on_file_delete:
		return

	if not file_doc.file_url or not file_doc.file_url.startswith("/s3/"):
		return

	s3_key = file_doc.file_url.replace("/s3/", "", 1)

	from erpnext_s3_integration.s3_client import S3Client

	s3_client = S3Client()
	s3_client.delete_object(s3_key)
