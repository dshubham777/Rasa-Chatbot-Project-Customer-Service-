"""Rename table name `template` and column `template`.

Reason:
In Rasa Open Source, `templates` in the domain got renamed to `responses`. Rasa X should
follow this rename.

Revision ID: 36fa577b374d
Revises: b092b0fe590d

"""
from alembic import op

import rasax.community.database.schema_migrations.alembic.utils as migration_utils

from typing import Text


# revision identifiers, used by Alembic.
revision = "36fa577b374d"
down_revision = "b092b0fe590d"
branch_labels = None
depends_on = None


OLD_TABLE_NAME = "template"
NEW_TABLE_NAME = "response"
OLD_COLUMN_NAME = "template"
NEW_COLUMN_NAME = "response_name"


def upgrade():
    rename_column(OLD_TABLE_NAME, OLD_COLUMN_NAME, NEW_COLUMN_NAME)
    rename_table(OLD_TABLE_NAME, NEW_TABLE_NAME)

    # add sequence for Oracle DB compatibility
    migration_utils.create_sequence(NEW_TABLE_NAME)


def downgrade():
    rename_column(NEW_TABLE_NAME, NEW_COLUMN_NAME, OLD_COLUMN_NAME)
    rename_table(NEW_TABLE_NAME, OLD_TABLE_NAME)


def rename_column(
    table_name: Text, old_column_name: Text, new_column_name: Text
) -> None:
    if migration_utils.table_has_column(table_name, old_column_name):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column(old_column_name, new_column_name=new_column_name)


def rename_table(old_table_name: Text, new_table_name: Text) -> None:
    if migration_utils.table_exists(old_table_name):
        op.rename_table(old_table_name, new_table_name)
