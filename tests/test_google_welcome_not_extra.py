"""Google's welcome mail in a freshly made target account is not an extra; every other unmatched email is."""
import verify_sample as V


def test_only_googles_exact_welcome_sender_is_recognised():
    assert V._google_welcome("Gmail Team <mail-noreply@google.com>")
    assert V._google_welcome("mail-noreply@google.com")
    assert not V._google_welcome("Google <no-reply@accounts.google.com>")
    assert not V._google_welcome("someone <mail-noreply@google.com.evil.example>")
    assert not V._google_welcome("")


def test_the_check_still_reports_every_other_unmatched_email():
    import inspect
    src = inspect.getsource(V.Verifier)
    i = src.index("_google_welcome(head.get")
    assert "mid not in mapped_msgids and _google_welcome" in src[i - 60:i + 40]
    assert 'res["extras"]).append' in src[i:]
