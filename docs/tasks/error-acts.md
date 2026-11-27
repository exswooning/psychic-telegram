# Known error acts: row 2

Status is offline fake-driver evidence unless a row explicitly says otherwise. No tenant run is part of this work.

| `known_issues` id | Act / outcome |
|---|---|
| `gmail.labels_added` | Pending: seed a received message whose sender matches the source's old domain; offline act and judge test pending. |
| `gmail.duplicate` | Pending: seed the repeated Message-ID case; offline act and judge test pending. |
| `gmail.no_message_id` | Pending: seed a message without Message-ID; offline act and judge test pending. |
| `gmail.body_link_copy` | Pending: seed link mail whose body link resolves to a genuine copy; offline act and judge test pending. |
| `gmail.link_owner_not_migrated` | Pending: seed a Drive link owned by a user outside the run; offline act and judge test pending. |
| `gmail.link_scan_encrypted` | Pending: seed encrypted link mail; offline act and judge test pending. |
| `run.mail_owed_to_dms` | Pending: seed a non-link message assigned to DMS; offline act and judge test pending. |
| `drive.shortcut_target` | Pending: seed a shortcut to another user's mapped file; offline act and judge test pending. |
| `drive.item_absent` | Pending: seed a shortcut whose owner has no migrated copy; offline act and judge test pending. |
| `drive.group_grant` | Pending: seed a file shared with a Google Group unavailable on the target; offline act and judge test pending. |
| `drive.embedded_images` | Pending: seed a Doc with embedded images; offline act and judge test pending. |
| `run.form_rebuild` | Pending: seed a Form that takes the rebuild path; offline act and judge test pending. |
| `drive.comments` | Pending: seed a comment thread; offline act and judge test pending. |
| `run.comment_differs` | Pending: seed a comment whose copied content differs; offline act and judge test pending. |
| `drive.comments_unreadable` | Pending: seed a comment target the verifier cannot read; offline act and judge test pending. |
| `drive.sheet_too_large` | Pending: seed a Sheet over the verifier's read budget; offline act and judge test pending. |
| `tasks.untitled` | Pending: seed a task with an empty title; offline act and judge test pending. |
| `run.malware_flag` | Pending: seed a file Google flags as malware, or document UNSEEDABLE with its reason; tests pending. |
| `drive.unmapped_source` | Pending: seed an outside-owned file in a user's folder; offline act and judge test pending. |
| `gmail.link_scan_fixed` | Pending: make a link-mail case that proves the fixed defect stays absent; offline act and judge test pending. |
| `drive.sheets_scope` | Pending: exercise Sheets through the required Drive scope and prove the finding stays absent; offline act and judge test pending. |
| `drive.office_compared_as_native` | Pending: seed an Office file and prove it is not compared as a native Google file; offline act and judge test pending. |
| `drive.comment_quote_escaped` | Pending: seed quoted comment text and prove the fixed defect stays absent; offline act and judge test pending. |
| `gmail.labels_missing` | Implemented: migrate a twin's Gmail, remove one label on b., verify and repair. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `gmail.unmapped_source` | Implemented: migrate a twin, add mail on a., verify and repair. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.mtime` | Implemented: migrate a twin, change the copied file's modified time on b., verify and repair. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.content_changed` | Implemented: migrate a twin, edit file bytes on a., verify, delta, then verify again. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.missing_on_target` | Implemented: migrate a twin, delete its copy on b., verify and judge the listed next step. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `drive.grant_missing` | Implemented: migrate a twin, remove a direct grant on b., verify and repair. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `contacts.unmapped_source` | Implemented: migrate a twin, add a contact on a., verify and judge the listed next step. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `contacts.missing_on_target` | Implemented: migrate a twin, delete a copied contact on b., verify and judge the listed next step. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |
| `tasks.list_missing` | Implemented: migrate a twin, delete a copied task list on b., verify and judge the listed next step. Offline fake-driver pass and unhandled FAIL assertions added; live remains unverified. |

