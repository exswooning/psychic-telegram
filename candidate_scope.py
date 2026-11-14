"""Pure validation of the one supported candidate and its Git object proof."""

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import re
import subprocess


def classification_only(original, candidate):
    try:
        before = ast.parse(original)
        after = ast.parse(candidate)
        old = next(
            n
            for n in before.body
            if isinstance(n, ast.FunctionDef) and n.name == "is_quota_error"
        )
        new = next(
            n
            for n in after.body
            if isinstance(n, ast.FunctionDef) and n.name == "is_quota_error"
        )
        if ast.dump(ast.Module(body=old.body, type_ignores=[])) == ast.dump(
            ast.Module(body=new.body, type_ignores=[])
        ):
            return False
        forbidden = (
            ast.Import,
            ast.ImportFrom,
            ast.Global,
            ast.Nonlocal,
            ast.Lambda,
            ast.ClassDef,
            ast.AsyncFunctionDef,
            ast.While,
            ast.For,
            ast.AsyncFor,
            ast.With,
            ast.AsyncWith,
            ast.Try,
            ast.Raise,
            ast.Delete,
            ast.Yield,
            ast.YieldFrom,
            ast.Await,
            ast.NamedExpr,
        )
        for statement in new.body:
            for node in ast.walk(statement):
                if isinstance(node, forbidden) or isinstance(
                    node, (ast.FunctionDef, ast.comprehension)
                ):
                    return False
                if isinstance(node, ast.Call) and not (
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"getattr", "_extract_reason", "str"}
                ):
                    return False
                if isinstance(node, ast.Name) and node.id.startswith("__"):
                    return False
                if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
                    return False
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and node.value.startswith("__")
                ):
                    return False
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    if any(not isinstance(target, ast.Name) for target in targets):
                        return False
        normalized = copy.deepcopy(after)
        node = next(
            n
            for n in normalized.body
            if isinstance(n, ast.FunctionDef) and n.name == "is_quota_error"
        )
        node.body = copy.deepcopy(old.body)
        return ast.dump(before, include_attributes=False) == ast.dump(
            normalized, include_attributes=False
        )
    except (SyntaxError, StopIteration, ValueError, TypeError):
        return False


def object_hash(kind, data):
    return hashlib.sha1(
        kind.encode() + b" " + str(len(data)).encode() + b"\0" + data
    ).hexdigest()


def _tree(raw):
    out = {}
    while raw:
        header, sep, rest = raw.partition(b"\0")
        if not sep or len(rest) < 20:
            raise ValueError("Malformed Git tree")
        mode, sep, name = header.partition(b" ")
        if not sep or not name or b"/" in name or name in out:
            raise ValueError("Invalid Git tree entry")
        out[name] = (mode, rest[:20].hex())
        raw = rest[20:]
    return out


def _commit(raw):
    header, sep, _ = raw.partition(b"\n\n")
    if not sep:
        raise ValueError("Malformed Git commit")
    trees = [
        x[5:].decode("ascii") for x in header.split(b"\n") if x.startswith(b"tree ")
    ]
    parents = [
        x[7:].decode("ascii") for x in header.split(b"\n") if x.startswith(b"parent ")
    ]
    if (
        len(trees) != 1
        or len(parents) != 1
        or any(not re.fullmatch("[0-9a-f]{40}", x) for x in trees + parents)
    ):
        raise ValueError("Candidate must have one parent and one tree")
    return trees[0], parents[0]


def build_proof(tree, candidate, base):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tree), *args])

    def obj(kind, revision):
        return git("cat-file", kind, revision)

    data = {
        "commit": obj("commit", candidate),
        "tree": obj("tree", candidate + "^{tree}"),
        "parent_commit": obj("commit", base),
        "parent_tree": obj("tree", base + "^{tree}"),
        "tests_tree": obj("tree", candidate + ":tests"),
        "parent_tests_tree": obj("tree", base + ":tests"),
        "candidate_blob": obj("blob", candidate + ":verify_gmail_quota.py"),
        "parent_blob": obj("blob", base + ":verify_gmail_quota.py"),
    }
    return {k: base64.b64encode(v).decode("ascii") for k, v in data.items()}


def validate_proof(proof, candidate, installed_base):
    names = {
        "commit",
        "tree",
        "parent_commit",
        "parent_tree",
        "tests_tree",
        "parent_tests_tree",
        "candidate_blob",
        "parent_blob",
    }
    if not isinstance(proof, dict) or set(proof) != names:
        raise ValueError("Complete Git object proof is required")
    if any(not isinstance(proof[k], str) or len(proof[k]) > 700000 for k in names):
        raise ValueError("Git object proof exceeds size limit")
    try:
        data = {k: base64.b64decode(proof[k], validate=True) for k in names}
    except (TypeError, ValueError):
        raise ValueError("Invalid Git object proof encoding") from None
    if any(len(v) > 500000 for v in data.values()):
        raise ValueError("Git object proof exceeds size limit")
    if object_hash("commit", data["commit"]) != candidate:
        raise ValueError("Candidate commit proof does not match")
    tree, parent = _commit(data["commit"])
    if (
        parent != installed_base
        or object_hash("commit", data["parent_commit"]) != parent
    ):
        raise ValueError("Candidate parent is not the installed release")
    if object_hash("tree", data["tree"]) != tree:
        raise ValueError("Candidate tree proof does not match")
    parent_header = data["parent_commit"].split(b"\n\n", 1)[0].split(b"\n")
    parent_trees = [
        line[5:].decode("ascii") for line in parent_header if line.startswith(b"tree ")
    ]
    if (
        len(parent_trees) != 1
        or object_hash("tree", data["parent_tree"]) != parent_trees[0]
    ):
        raise ValueError("Parent tree proof does not match")
    old, new = _tree(data["parent_tree"]), _tree(data["tree"])
    if set(old) != set(new) or any(
        old[k] != new[k] for k in old if k not in {b"tests", b"verify_gmail_quota.py"}
    ):
        raise ValueError("Candidate changes files outside the supported scenario")
    for entries, key in ((old, "parent_tests_tree"), (new, "tests_tree")):
        if entries.get(b"tests") != (b"40000", object_hash("tree", data[key])):
            raise ValueError("Tests tree proof does not match")
    old_tests, new_tests = _tree(data["parent_tests_tree"]), _tree(data["tests_tree"])
    if any(new_tests.get(name) != value for name, value in old_tests.items()):
        raise ValueError("Existing tests may not be changed")
    added = set(new_tests) - set(old_tests)
    if not added or any(
        not re.fullmatch(rb"test_[A-Za-z0-9_]+\.py", name)
        or new_tests[name][0] != b"100644"
        for name in added
    ):
        raise ValueError("A new regression test is required")
    for entries, key in ((old, "parent_blob"), (new, "candidate_blob")):
        if entries.get(b"verify_gmail_quota.py") != (
            b"100644",
            object_hash("blob", data[key]),
        ):
            raise ValueError("Gmail quota artifact proof does not match")
    if not classification_only(data["parent_blob"], data["candidate_blob"]):
        raise ValueError("Candidate is outside the approved pure classifier scenario")
    return hashlib.sha256(data["candidate_blob"]).hexdigest()
