"""The one piece of the multi-scraper Chrome pool manager this scraper uses:
`create_scope()`, the lock held while an ad-hoc browser launches (the token
minter builds one PatchrightDriver)."""
import threading
from contextlib import contextmanager

_create_lock = threading.Lock()


@contextmanager
def create_scope():
    with _create_lock:
        yield
