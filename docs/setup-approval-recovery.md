# Guided setup approval and recovery

The setup browser recognizes three specific Google screens: the new Workspace account acknowledgement, Google Cloud SDK's Cloud-access consent, and the first-use Cloud Platform terms dialog. These are not generic Next/Allow buttons.

For a recognized screen, the authenticated setup card and Jobs page show the account, complete bounded notice, action and expiring review request. After an operator reviews and approves that exact step, the server continues automatically. A separate decision is needed if the account, application, terms or permissions change. Decline stops the step.

The authenticated API writes an audited response for the selected migration's live setup only. The browser consumes it once and rechecks the Google origin, account and notice fingerprint before clicking a single known button. Stale, expired, duplicated or mismatched responses cannot authorize a click. The UI resets review state on a changed request and ignores late responses from older requests.

The Google Cloud SDK client ID is fixed. OAuth URLs and verification codes are not copied into the review notice. Arbitrary origins, unknown applications and notices too long to display completely fall back to the existing on-device path. Generic legal/access button clicks have been removed from automatic navigation.

On-device links now have an expiry state. A timeout gives a clear fresh-link instruction instead of returning a tail of the OAuth URL as the error. Auth output redacts complete URLs and sensitive parameters. A code still belongs to the gcloud process that generated its PKCE request; it cannot revive a terminated setup.

MFA, CAPTCHA, password changes, new billing decisions and unsupported Google screens retain an operator action or on-device handoff. Once the service account and delegation are ready, ordinary migrations reuse those credentials rather than repeating Cloud setup for each user.

Qualification uses fake browser pages, isolated files and authenticated API/UI tests. Passing these checks does not establish live Google behavior for every locale or future page revision. The broader autonomous incident/agent system is a separate proposed build in autonomous-migration-assurance-plan.md.

The separate first-use Google Cloud Platform terms dialog is reviewed in the same account/run-specific flow after sign-in. Its country and full dialog text are shown. Only its terms checkbox and Agree and continue are submitted after approval; email promotions and free trials are not selected.
