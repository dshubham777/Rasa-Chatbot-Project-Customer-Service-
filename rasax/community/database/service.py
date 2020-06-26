from typing import Optional, List

from sqlalchemy.ext.declarative import DeclarativeMeta
from sqlalchemy.orm import Session, Query

from rasax.community.database.base import Base


class DbService:
    """Base class for SQL database services.

    Implements most common SQL operations on its `session` attribute.
    """

    def __init__(self, session: Optional[Session] = None):
        self.session = session

    def commit(self) -> None:
        """Commit current transaction."""

        self.session.commit()

    def add(self, instance: Base) -> None:
        """Place an object in `session`."""

        self.session.add(instance)

    def flush(self) -> None:
        """Flush all changes to the database."""

        self.session.flush()

    def delete(self, instance: Base) -> None:
        """Delete an instance."""

        self.session.delete(instance)

    def query(self, *entities: DeclarativeMeta) -> Query:
        """Returns a `Query` object within `session`."""

        return self.session.query(*entities)

    def bulk_save_objects(self, objects: List[Base]) -> None:
        """Performs a bulk save of `objects`."""

        self.session.bulk_save_objects(objects)

    def merge(self, instance: Base) -> None:
        """Perform a merge of `instance` within the current `session`."""

        self.session.merge(instance)

    def rollback(self) -> None:
        """Roll back current transaction."""

        self.session.rollback()

    def add_all(self, instances: List[Base]) -> None:
        """Add a list of mapped objects."""

        self.session.add_all(instances)

    def delete_all(self, instances: List[Base]) -> None:
        """Delete a list of mapped objects."""

        if instances:
            for element in instances:
                self.delete(element)
