from contextlib import contextmanager
import logging
from pathlib import Path
from typing import (
    Generator,
    TYPE_CHECKING,
)

from eth_utils import ValidationError

from hvm.db.diff import (
    DBDiffTracker,
    DiffMissingError,
)
from .base import (
    BaseAtomicDB,
    BaseDB,
)

if TYPE_CHECKING:
    import plyvel  # noqa: F401


class LevelDB(BaseAtomicDB):
    logger = logging.getLogger("hls.db.backends.LevelDB")

    # Creates db as a class variable to avoid level db lock error
    def __init__(self, db_path: Path = None) -> None:
        if not db_path:
            raise TypeError("Please specifiy a valid path for your database.")
        try:
            import plyvel  # noqa: F811
        except ImportError:
            raise ImportError(
                "LevelDB requires the plyvel library which is not available for import."
            )
        self.db_path = db_path
        self.db = plyvel.DB(str(db_path), create_if_missing=True, error_if_exists=False)

    def __getitem__(self, key: bytes) -> bytes:
        v = self.db.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def __setitem__(self, key: bytes, value: bytes) -> None:
        self.db.put(key, value)

    def _exists(self, key: bytes) -> bool:
        return self.db.get(key) is not None

    def __delitem__(self, key: bytes) -> None:
        self.db.delete(key)

    @contextmanager
    def atomic_batch(self) -> Generator['LevelDBWriteBatch', None, None]:
        with self.db.write_batch(transaction=True) as atomic_batch:
            readable_batch = LevelDBWriteBatch(self, atomic_batch)
            try:
                yield readable_batch
            finally:
                readable_batch.decommission()

    def destroy_db(self):
        try:
            from plyvel import destroy_db
        except ImportError:
            raise ImportError("LevelDB requires the plyvel \
                               library which is not available for import.")
        self.db.close()
        destroy_db(str(self.db_path))


class LevelDBWriteBatch(BaseDB):
    """
    A native leveldb write batch does not permit reads on the in-progress data.
    This class fills that gap, by tracking the in-progress diff, and adding
    a read interface.
    """
    logger = logging.getLogger("hls.db.backends.LevelDBWriteBatch")

    def __init__(self, original_read_db: BaseDB, write_batch: 'plyvel.WriteBatch') -> None:
        self._original_read_db = original_read_db
        self._write_batch = write_batch
        # keep track of the temporary changes made
        self._track_diff = DBDiffTracker()

    def __getitem__(self, key: bytes) -> bytes:
        if self._track_diff is None:
            raise ValidationError("Cannot get data from a write batch, out of context")

        try:
            changed_value = self._track_diff[key]
        except DiffMissingError as missing:
            if missing.is_deleted:
                raise KeyError(key)
            else:
                return self._original_read_db[key]
        else:
            return changed_value

    def __setitem__(self, key: bytes, value: bytes) -> None:
        if self._track_diff is None:
            raise ValidationError("Cannot set data from a write batch, out of context")

        self._write_batch.put(key, value)
        self._track_diff[key] = value

    def _exists(self, key: bytes) -> bool:
        if self._track_diff is None:
            raise ValidationError("Cannot test data existance from a write batch, out of context")

        try:
            self._track_diff[key]
        except DiffMissingError as missing:
            if missing.is_deleted:
                return False
            else:
                return key in self._original_read_db
        else:
            return True

    def __delitem__(self, key: bytes) -> None:
        if self._track_diff is None:
            raise ValidationError("Cannot delete data from a write batch, out of context")

        self._write_batch.delete(key)
        del self._track_diff[key]

    def decommission(self) -> None:
        """
        Prevent any further actions to be taken on this write batch, called after leaving context
        """
        self._track_diff = None