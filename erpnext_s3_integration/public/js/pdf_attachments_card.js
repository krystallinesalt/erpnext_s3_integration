frappe.provide("erpnext_s3_integration.pdf_attachments");

erpnext_s3_integration.pdf_attachments.DOCTYPES = [
	"Purchase Invoice",
	"Purchase Credit Note",
	"Purchase Order",
	"Purchase Receipt",
	"Sales Invoice",
	"Sales Credit Note",
	"Supplier Quotation",
];

erpnext_s3_integration.pdf_attachments.render = function (frm) {
	const field = frm.get_field("attachments_card_html");
	if (!field) return;

	const $wrapper = $(field.$wrapper).empty();

	if (frm.is_new()) {
		$wrapper.html(
			`<div class="text-muted small">${__("Save this document before attaching a PDF.")}</div>`
		);
		return;
	}

	frappe.call({
		method: "erpnext_s3_integration.attachment_tracking.get_attachment_history",
		args: { doctype: frm.doctype, docname: frm.docname },
		callback: (r) => {
			if (r.message) {
				erpnext_s3_integration.pdf_attachments.draw(frm, $wrapper, r.message);
			}
		},
	});
};

erpnext_s3_integration.pdf_attachments.draw = function (frm, $wrapper, data) {
	const can_write = !!(frm.perm && frm.perm[0] && frm.perm[0].write);

	const active_html = data.active
		? `<div class="d-flex align-items-center justify-content-between" style="padding:6px 0;">
			<a href="${frappe.utils.escape_html(data.active.file_url)}" target="_blank">
				${frappe.utils.escape_html(data.active.file_name)}
			</a>
			${
				can_write
					? `<button type="button" class="btn btn-xs btn-default pdf-attach-delete" data-name="${data.active.name}">
						${__("Delete")}
					</button>`
					: ""
			}
		   </div>`
		: `<div class="text-muted small" style="padding:6px 0;">${__("No attachment")}</div>`;

	const upload_html = can_write
		? `<button type="button" class="btn btn-xs btn-primary pdf-attach-upload" style="margin-top:4px;">
			${data.active ? __("Replace PDF") : __("Attach PDF")}
		   </button>`
		: "";

	const deleted_rows = (data.deleted || [])
		.map(
			(row) => `
			<div class="d-flex align-items-center justify-content-between" style="padding:4px 0;">
				<span class="text-muted">${frappe.utils.escape_html(row.file_name)}</span>
				<span>
					<a href="${frappe.utils.escape_html(row.file_url)}" target="_blank" class="btn btn-xs btn-default">
						${__("View")}
					</a>
					${
						can_write
							? `<button type="button" class="btn btn-xs btn-default pdf-attach-restore" data-name="${row.name}">
								${__("Restore")}
							</button>`
							: ""
					}
				</span>
			</div>`
		)
		.join("");

	$wrapper.html(`
		<div class="pdf-attachments-card" style="border:1px solid var(--border-color); border-radius:var(--border-radius); padding:10px 12px;">
			${active_html}
			${upload_html}
			<div class="text-muted small" style="margin-top:10px; text-transform:uppercase; font-weight:600;">
				${__("Deleted")}
			</div>
			${deleted_rows || `<div class="text-muted small" style="padding:4px 0;">${__("None")}</div>`}
		</div>
	`);

	$wrapper.find(".pdf-attach-delete").on("click", function () {
		frappe.call({
			method: "erpnext_s3_integration.attachment_tracking.soft_delete_pdf_attachment",
			args: { name: $(this).attr("data-name") },
			freeze: true,
			callback: () => frm.reload_doc(),
		});
	});

	$wrapper.find(".pdf-attach-restore").on("click", function () {
		frappe.call({
			method: "erpnext_s3_integration.attachment_tracking.restore_pdf_attachment",
			args: { name: $(this).attr("data-name") },
			freeze: true,
			callback: () => frm.reload_doc(),
		});
	});

	$wrapper.find(".pdf-attach-upload").on("click", function () {
		new frappe.ui.FileUploader({
			doctype: frm.doctype,
			docname: frm.docname,
			fieldname: "pdf_copy",
			restrictions: { allowed_file_types: [".pdf", "application/pdf"] },
			on_success: (file_doc) => {
				frappe.call({
					method: "erpnext_s3_integration.attachment_tracking.finalize_pdf_attachment",
					args: { doctype: frm.doctype, docname: frm.docname, file: file_doc.name },
					freeze: true,
					callback: () => frm.reload_doc(),
				});
			},
		});
	});
};

erpnext_s3_integration.pdf_attachments.DOCTYPES.forEach((doctype) => {
	frappe.ui.form.on(doctype, {
		refresh(frm) {
			erpnext_s3_integration.pdf_attachments.render(frm);
		},
	});
});
