"""Add slot name and value to ConversationEvent

Reason:
Add 'slot_name' and 'slot_value' columns to the 'conversation_event'
table. Using these, we can use these two values to filter conversation events
by slot names or values.

Revision ID: 6738be716c3f
Revises: 0b35090e53cf

"""
import json

from alembic import op
import sqlalchemy as sa

from rasa.core.constants import REQUESTED_SLOT
import rasax.community.database.schema_migrations.alembic.utils as migration_utils


# revision identifiers, used by Alembic.
revision = "6738be716c3f"
down_revision = "1a712f7b70e9"
branch_labels = None
depends_on = None

TABLE_NAME = "conversation_event"
COLUMNS = {
    "slot_name": {"index": "conversation_slot_name_index", "type": sa.String(255),},
    "slot_value": {"index": "conversation_slot_value_index", "type": sa.Text(),},
}
BULK_SIZE = 500


def upgrade():
    for column, info in COLUMNS.items():
        migration_utils.create_column(TABLE_NAME, sa.Column(column, info["type"]))

    bind = op.get_bind()
    session = sa.orm.Session(bind=bind)

    events_table = migration_utils.get_reflected_table(TABLE_NAME, session)

    for row in session.query(events_table).yield_per(BULK_SIZE):
        if row.type_name != "slot":
            continue

        event = json.loads(row.data)
        slot_name = event.get("name")
        slot_value = event.get("value")

        if slot_name == REQUESTED_SLOT:
            continue

        query = (
            sa.update(events_table)
            .where(events_table.c.id == row.id)
            .values(
                # Sort keys so that equivalent values can be discarded using DISTINCT
                # in an SQL query
                slot_name=slot_name,
                slot_value=json.dumps(slot_value, sort_keys=True),
            )
        )
        session.execute(query)

    with op.batch_alter_table(TABLE_NAME) as batch_op:
        # Only create index for `slot_name` column, see migration 3fbc8790762e
        # to see why.
        batch_op.create_index(COLUMNS["slot_name"]["index"], ["slot_name"])

    session.commit()


def downgrade():
    with op.batch_alter_table(TABLE_NAME) as batch_op:
        batch_op.drop_index(COLUMNS["slot_name"]["index"])

    for column in COLUMNS:
        migration_utils.drop_column(TABLE_NAME, column)
