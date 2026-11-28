"""Deploy drift: the box against HEAD, by content for Python and by build time for the UI; the inbox
hears about it only when the drift changes (live, 7 Nov 2026: /app ran a UI ten days old, unnoticed)."""
import hashlib

from monitor import watch as W

FILES = {"a.py": b"print(1)\n", "b.py": b"print(2)\n"}


def git(*args):
    if args[0] == "ls-files":
        return b"a.py b.py sub/c.py"
    if args[0] == "show":
        return FILES[args[1].split(":", 1)[1]]
    if args[0] == "log":
        return b"2000\n"
    return b"abc1234\n"


def box_with(a_content, built):
    def box(cmd):
        if cmd == "pyhash":
            return (f"{hashlib.sha256(a_content).hexdigest()}  a.py\n"
                    f"{hashlib.sha256(FILES['b.py']).hexdigest()}  b.py\n")
        return f"{built}\n"
    return box


def test_matching_box_is_not_drift():
    d = W.drift(box=box_with(FILES["a.py"], 3000), git=git)
    assert d["drifted"] is False and d["differ"] == [] and d["ui"] == {}


def test_changed_file_and_old_ui_are_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "INBOX", str(tmp_path / "INBOX.md"))
    d = W.drift(box=box_with(b"old\n", 1000), git=git)
    assert d["differ"] == ["a.py"] and d["ui"]["lastUiCommit"]
    state = {}
    assert W.note_drift(state, d) is True
    assert W.note_drift(state, d) is False                      # same drift: no second line
    assert (tmp_path / "INBOX.md").read_text().count("DEPLOY DRIFT") == 1
