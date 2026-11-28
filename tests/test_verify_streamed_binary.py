"""Binary verification drains Drive media through bounded hash chunks."""
import tracemalloc

import verify_sample as V


class _MediaRequest:
    def __init__(self, size, changed_at=None):
        self.size, self.changed_at, self.offset = size, changed_at, 0


class _Service:
    def __init__(self, size, changed_at=None):
        self.request = _MediaRequest(size, changed_at)
    def files(self): return self
    def get_media(self, **kwargs): return self.request


class _ChunkDownloader:
    calls = 0
    max_chunk = 0
    def __init__(self, sink, request, chunksize=-1):
        self.sink, self.request, self.chunk_size = sink, request, chunksize
    def next_chunk(self):
        type(self).calls += 1
        start = self.request.offset
        size = min(self.chunk_size, self.request.size - start)
        if size <= 0:
            return None, True
        type(self).max_chunk = max(type(self).max_chunk, size)
        chunk = bytearray(b"a" * size)
        changed = self.request.changed_at
        if changed is not None and start <= changed < start + size:
            chunk[changed - start] = ord("b")
        self.sink.write(chunk)
        self.request.offset += size
        return None, self.request.offset == self.request.size


def test_a_differing_byte_is_found_without_retaining_two_file_bodies(monkeypatch):
    size = 3 * 1024 * 1024 + 17
    changed_at = 2 * 1024 * 1024 + 7
    monkeypatch.setattr(V, "MediaIoBaseDownload", _ChunkDownloader)
    _ChunkDownloader.calls = _ChunkDownloader.max_chunk = 0
    v = object.__new__(V.Verifier)
    v._x = lambda fn, **kwargs: fn()
    source, target = _Service(size), _Service(size, changed_at)

    tracemalloc.start()
    try:
        a, b = v._download_digest(source, "source"), v._download_digest(target, "target")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert (a["size"], b["size"]) == (size, size)
    assert a["sha256"] != b["sha256"]
    assert _ChunkDownloader.max_chunk == 1 << 20
    assert _ChunkDownloader.calls == 8
    assert peak < 5 * 1024 * 1024
