"""This migration is left empty because of issue with 0.25.0 release.

After merging 0.24.x to the master this migration was originally never applied.
Check migrations: 8a260b1a797a

Revision ID: 443af39e50e3
Revises: acd80348b093

"""
from alembic import op
import sqlalchemy as sa

import rasax.community.database.schema_migrations.alembic.utils as migration_utils

# revision identifiers, used by Alembic.
revision = "443af39e50e3"
down_revision = "acd80348b093"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
