"""Add index for `created_by` foreign key.

Reason:
We are filtering on `created_by` in each conversation screen request,
so indexing `created_by` will speed up the query execution.

Revision ID: ac3fba1c2b86
Revises: 7b2497cd88dc

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "ac3fba1c2b86"
down_revision = "7b2497cd88dc"
branch_labels = None
depends_on = None


NEW_INDEX_NAME = "conversation_created_by_idx"


def upgrade():
    with op.batch_alter_table("conversation") as batch_op:
        batch_op.create_index(NEW_INDEX_NAME, ["created_by"])


def downgrade():
    with op.batch_alter_table("conversation") as batch_op:
        batch_op.drop_index(NEW_INDEX_NAME)
