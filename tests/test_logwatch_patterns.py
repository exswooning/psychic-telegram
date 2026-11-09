"""Plan item 5: the log watcher's patterns, tested against real lines from account 3's run (9 Nov 2026)."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "monitor"))
import logwatch as L  # noqa: E402

CATCH = [
    '    ! audit-ab2-10310305@t.sarafgloabalexim.com chat: HttpError: <HttpError 403 when requesting https://chat.googleapis.com/v1/spaces?pageSize=100&alt=json returned "Request had insuffi',
    '2026-11-09 05:42:44,177 WARNING [drive-ward18_5] googleapiclient.http: Encountered 403 Forbidden with reason "insufficientFilePermissions"',
    '2026-11-09 05:45:21,355 WARNING [ward8         ] googleapiclient.http: Encountered 403 Forbidden with reason "teamDriveMembershipRequired"',
    '2026-11-09T07:12:38.000Z INCIDENT #37 error crashed account=3 job=migrate :: migrate exited with code 3',
    '2026-11-09 06:54:29,758 WARNING [verify-drive-file_1] verify_sample: drive comparison cache not written for 1j9Z: database is locked',
    'Traceback (most recent call last):',
    '2026-11-10 20:40:00,000 ERROR   [parity] parity: ward7@s.x: drive: drive mtime x2',
    '2026-11-10 23:00:00,000 INFO    [parity] parity: PARITY REACHED for all 45 users -- run the full tenant deep check',
]
IGNORE = [
    "acceptance: verify: a@s | drive finished in 3.0s; completion=COMPLETE; verdict=IDENTICAL; 0 checked, 0 identical, 0 difference(s), 0 missing, 0 incomplete/error(s), 3 API",
    "2026-11-09 05:38:42,375 INFO [drive-x_6] drive_engine: [x@s] native_api recovered  Form after download_upload failed",
    "2026-11-09 05:43:26,534 INFO [drive-x_6] drive_engine: [x@s] download_upload recovered a.pdf after server_side failed",
    "2026-11-09 06:25:10,994 WARNING [drive-ward2_0 ] resilience: Google refused drive.files.move: HTTP 403 userRateLimitExceeded -- User rate limit exceeded.",
    "[dwd] import status: In progress {'Emails failed': 0}",
    "copied 403 files",
    "2026-11-10 20:40:00,000 INFO    [parity] parity: ward7@s.x: drive: at parity",
]


def test_the_watcher_catches_what_it_must():
    missed = [line for line in CATCH if not (L.TERMS.search(line) and not L.QUIET.search(line))]
    assert missed == []


def test_and_stays_quiet_on_what_is_handled():
    noisy = [line for line in IGNORE if L.TERMS.search(line) and not L.QUIET.search(line)]
    assert noisy == []
