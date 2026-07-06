from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils.file_manager import get_content_hash

from erpnext_s3_integration.attachment_tracking import (
	get_attachment_history,
	reject_if_duplicate_pdf_attachment,
	restore_pdf_attachment,
	soft_delete_pdf_attachment,
	sync_after_upload,
)
from erpnext_s3_integration.file_hooks import on_trash, rename_attached_files_for_parent


class TestAttachmentTracking(FrappeTestCase):
	def setUp(self):
		settings = frappe.get_single("S3 Integration Settings")
		if not settings.enable_attachments_s3:
			settings.enable_attachments_s3 = 1
			settings.flags.ignore_mandatory = True
			settings.save(ignore_permissions=True)

		self.parent = frappe.get_doc({"doctype": "Purchase Invoice"})
		self.parent.flags.ignore_mandatory = True
		self.parent.insert(ignore_permissions=True)
		self.addCleanup(
			lambda: frappe.delete_doc(
				"Purchase Invoice", self.parent.name, force=1, ignore_permissions=True
			)
		)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _make_file(self, file_name, file_url, content_hash=None):
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"file_url": file_url,
				"attached_to_doctype": "Purchase Invoice",
				"attached_to_name": self.parent.name,
				"attached_to_field": "pdf_copy",
				"content_hash": content_hash,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		self.addCleanup(
			lambda: frappe.delete_doc("File", file_doc.name, force=1, ignore_permissions=True)
		)
		return file_doc

	def test_sync_after_upload_activates_and_retires_previous(self):
		first = self._make_file("a.pdf", "/s3/a.pdf")
		sync_after_upload(first)

		history = get_attachment_history("Purchase Invoice", self.parent.name)
		self.assertEqual(history["active"].file, first.name)
		self.assertEqual(len(history["deleted"]), 0)

		second = self._make_file("b.pdf", "/s3/b.pdf")
		sync_after_upload(second)

		history = get_attachment_history("Purchase Invoice", self.parent.name)
		self.assertEqual(history["active"].file, second.name)
		self.assertEqual(len(history["deleted"]), 1)
		self.assertEqual(history["deleted"][0].file, first.name)
		self.assertEqual(
			frappe.db.get_value("Purchase Invoice", self.parent.name, "pdf_copy"), second.file_url
		)

	def test_reject_if_duplicate_pdf_attachment_blocks_same_content_active_or_deleted(self):
		content = b"%PDF-1.4 sample content"
		content_hash = get_content_hash(content)

		existing_file = self._make_file("a.pdf", "/s3/a.pdf", content_hash=content_hash)
		tracking = frappe.get_doc(
			{
				"doctype": "S3 PDF Attachment",
				"attached_to_doctype": "Purchase Invoice",
				"attached_to_name": self.parent.name,
				"file": existing_file.name,
				"file_name": existing_file.file_name,
				"file_url": existing_file.file_url,
				"content_hash": content_hash,
				"status": "Active",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(
			lambda: frappe.delete_doc(
				"S3 PDF Attachment", tracking.name, force=1, ignore_permissions=True
			)
		)

		new_file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "a-duplicate.pdf",
				"attached_to_doctype": "Purchase Invoice",
				"attached_to_name": self.parent.name,
				"attached_to_field": "pdf_copy",
				"is_private": 1,
			}
		)

		with self.assertRaises(frappe.ValidationError):
			reject_if_duplicate_pdf_attachment(new_file_doc, content)

		frappe.db.set_value("S3 PDF Attachment", tracking.name, "status", "Deleted")
		with self.assertRaises(frappe.ValidationError):
			reject_if_duplicate_pdf_attachment(new_file_doc, content)

	def test_soft_delete_clears_active_and_moves_to_deleted(self):
		file_doc = self._make_file("a.pdf", "/s3/a.pdf")
		sync_after_upload(file_doc)
		tracking_name = frappe.db.get_value("S3 PDF Attachment", {"file": file_doc.name}, "name")

		soft_delete_pdf_attachment(tracking_name)

		self.assertIsNone(frappe.db.get_value("Purchase Invoice", self.parent.name, "pdf_copy"))
		history = get_attachment_history("Purchase Invoice", self.parent.name)
		self.assertIsNone(history["active"])
		self.assertEqual(len(history["deleted"]), 1)

	def test_restore_demotes_current_active(self):
		first = self._make_file("a.pdf", "/s3/a.pdf")
		sync_after_upload(first)
		first_tracking = frappe.db.get_value("S3 PDF Attachment", {"file": first.name}, "name")

		second = self._make_file("b.pdf", "/s3/b.pdf")
		sync_after_upload(second)

		restore_pdf_attachment(first_tracking)

		history = get_attachment_history("Purchase Invoice", self.parent.name)
		self.assertEqual(history["active"].file, first.name)
		self.assertEqual(len(history["deleted"]), 1)
		self.assertEqual(history["deleted"][0].file, second.name)
		self.assertEqual(
			frappe.db.get_value("Purchase Invoice", self.parent.name, "pdf_copy"), first.file_url
		)

	def test_soft_delete_and_restore_require_write_permission(self):
		file_doc = self._make_file("a.pdf", "/s3/a.pdf")
		sync_after_upload(file_doc)
		tracking_name = frappe.db.get_value("S3 PDF Attachment", {"file": file_doc.name}, "name")

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			soft_delete_pdf_attachment(tracking_name)
		with self.assertRaises(frappe.PermissionError):
			restore_pdf_attachment(tracking_name)

	@patch("erpnext_s3_integration.s3_client.S3Client.move_object")
	def test_unrelated_parent_save_does_not_resurrect_deleted_attachment(self, mock_move_object):
		# rename_attached_files_for_parent reprocesses every historically attached File on
		# every save of the parent (not just the file that was just uploaded) - an unrelated
		# resave must not flip a soft-deleted attachment back to Active.
		first = self._make_file("a.pdf", "/s3/a.pdf")
		sync_after_upload(first)
		second = self._make_file("b.pdf", "/s3/b.pdf")
		sync_after_upload(second)

		history = get_attachment_history("Purchase Invoice", self.parent.name)
		self.assertEqual(history["active"].file, second.name)

		rename_attached_files_for_parent(self.parent, "on_update")

		history = get_attachment_history("Purchase Invoice", self.parent.name)
		self.assertEqual(history["active"].file, second.name)
		self.assertEqual(len(history["deleted"]), 1)
		self.assertEqual(history["deleted"][0].file, first.name)
		self.assertEqual(
			frappe.db.get_value("Purchase Invoice", self.parent.name, "pdf_copy"),
			history["active"].file_url,
		)

	def test_on_trash_blocks_hard_delete_of_tracked_file_for_non_system_manager(self):
		file_doc = self._make_file("a.pdf", "/s3/a.pdf")
		sync_after_upload(file_doc)

		frappe.set_user("Guest")
		with self.assertRaises(frappe.ValidationError):
			on_trash(file_doc, "on_trash")
