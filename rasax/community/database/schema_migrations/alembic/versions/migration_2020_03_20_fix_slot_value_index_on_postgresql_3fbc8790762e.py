"""This migration is left empty because of issues with the 0.26.2 release.

Reason:
This migration has been replaced by another one, please see migration
ef93223786ba.

Revision ID: 3fbc8790762e
Revises: 304e0754a200

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "3fbc8790762e"
down_revision = "304e0754a200"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
