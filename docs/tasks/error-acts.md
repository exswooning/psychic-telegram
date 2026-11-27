# Known error acts: row 2

Status is offline fake-driver evidence unless a row explicitly says otherwise. No tenant run is part of this work.

| `known_issues` id | Act / outcome |
|---|---|
| `gmail.labels_added` | Implemented: seed a SENT-only message from c05's source address, migrate Gmail in engine mode, then verify/repair. The probe requires both `SENT` and the exact source sender. Offline fake-driver pass/unhandled FAIL and fixture/probe tests added; live remains unverified. |
| `gmail.duplicate` | Implemented: seed repeated Message-ID mail on c04, migrate Drive + Gmail in split mode, start the product DMS route, then verify. The judge reads `SUCCESS_SOURCE_DUPLICATE`; fake Driver asserts PASS with that ledger row and FAIL when unhandled. Live remains unverified. |
| `gmail.no_message_id` | Implemented: seed `mail_no_message_id_link` on c06, migrate Drive + Gmail in split mode, then verify. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `gmail.body_link_copy` | Implemented: seed the own-file link in `mail_shapes` on c04, migrate Drive + Gmail in split mode, then verify. Offline fake-driver pass/unhandled FAIL; live remains unverified. |
| `gmail.link_owner_not_migrated` | Implemented: seed `mail_unmigrated_peer_link` on c08 with a real shared peer-owned file, run the selected-user split migration, then verify the listed finding. Fixture and fake-driver PASS/FAIL tests added; live remains unverified. |
| `gmail.link_scan_encrypted` | Implemented: seed the S/MIME message in `mail_shapes` on c04, run split mode, and judge its `LINK_SCAN_ENCRYPTED` ledger row against the accepted gate. Fake Driver asserts PASS with evidence and FAIL when unhandled; live remains unverified. |
| `run.mail_owed_to_dms` | Implemented: use c08's real peer-owned link, run split mode, start the product DMS route, and verify Gmail. The judge reads `DELIVERED_BY_DMS`; fake Driver asserts PASS with that row and FAIL when unhandled. Live remains unverified. |
| `drive.shortcut_target` | Implemented: seed `drive_shortcuts` on c03, migrate Drive, then verify. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.item_absent` | Implemented: seed `colleague_shortcut` on c03, migrate Drive alone so the peer-owned item has no copy, then verify. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.group_grant` | Implemented: seed `sharing_extras` on c02, migrate Drive, then verify. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.embedded_images` | Implemented: seed `doc_with_image` on c03, migrate Drive, then verify. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `run.form_rebuild` | Implemented: seed `forms_with_image` on c09, migrate Drive, then verify the rebuild path. The source probe requires an actual image item on the named Form. Offline fake-driver PASS/unhandled FAIL and fixture/probe tests added; live remains unverified. |
| `drive.comments` | Implemented: seed `drive_comments` on c03, migrate Drive, then verify. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `run.comment_differs` | Implemented: seed `comment_actions` on c03, migrate Drive, then verify. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.comments_unreadable` | Implemented: seed `forms_basic` on c09, migrate Drive, then verify the Form comment endpoint. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.sheet_too_large` | Implemented: seed `big_sheet` on c03, migrate Drive, then verify. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `tasks.untitled` | Implemented: seed `tasks_shapes` on c07, migrate Tasks, then verify. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `run.malware_flag` | Implemented: seed the EICAR test file on c09, migrate Drive, then verify. The source probe requires Google's exact `cannotDownloadAbusiveFile` refusal; fixture presence alone does not count. Offline fake-driver pass/unhandled FAIL and source-probe tests added; live remains unverified. |
| `drive.unmapped_source` | UNSEEDABLE: the outside-owned file requires an account outside the source domain to upload into c02's folder; its credential is outside the authorized a./b. write scope. `tests/test_campaign_evidence.py` retains the exact source-probe test. |
| `gmail.link_scan_fixed` | Implemented: seed a Gmail message linking to a syntactically valid but missing Drive ID on c02; run Drive + Gmail split mode and verify the fixed 404 scan path. Source fixture, fake-driver PASS when absent and FAIL when it returns, plus the exact URL fixture test added; live remains unverified. |
| `drive.sheets_scope` | Implemented: seed the c03 comments Sheet, migrate Drive and verify it with the Drive scope. The source probe proves that Sheet exists; fake-driver asserts PASS when the scope finding is absent and FAIL when it returns. Live remains unverified. |
| `drive.office_compared_as_native` | Implemented: seed a minimal DOCX on c07, migrate Drive and verify it as an Office file. Offline fake-driver PASS/FAIL and a DOCX package/MIME fixture test added; live remains unverified. |
| `drive.comment_quote_escaped` | Implemented: seed the quoted comment thread on c03, migrate Drive and verify. The source quote probe proves the condition exists; fake-driver asserts PASS when the fixed finding stays absent and FAIL when it returns. Live remains unverified. |
| `gmail.labels_missing` | Implemented: migrate a twin's Gmail, remove one label on b., verify and repair. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `gmail.unmapped_source` | Implemented: migrate a twin, add mail on a., verify and repair. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.mtime` | Implemented: migrate a twin, change the copied file's modified time on b., verify and repair. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.content_changed` | Implemented: migrate a twin, edit file bytes on a., verify, delta, then verify again. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.missing_on_target` | Implemented: migrate a twin, delete its copy on b., verify and judge the listed next step. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.grant_missing` | Implemented: migrate a twin, remove a direct grant on b., verify and repair. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `contacts.unmapped_source` | Implemented: migrate a twin, add a contact on a., verify and judge the listed next step. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `contacts.missing_on_target` | Implemented: migrate a twin, delete a copied contact on b., verify and judge the listed next step. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `tasks.list_missing` | Implemented: migrate a twin, delete a copied task list on b., verify and judge the listed next step. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |

