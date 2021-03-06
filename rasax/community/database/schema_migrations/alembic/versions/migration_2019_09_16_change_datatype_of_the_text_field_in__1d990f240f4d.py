"""Change datatype of the `text` column in the table `template` from `String` to `Text`

Reason:
The template text should be allowed to have arbitrary length. For this reason,
`Text` is the more suitable datatype instead of varchar.

Revision ID: 1d990f240f4d
Revises: 4daabca814ee

"""
from alembic import op
import sqlalchemy as sa
import rasax.community.database.schema_migrations.alembic.utils as migration_utils


# revision identifiers, used by Alembic.
revision = "1d990f240f4d"
down_revision = "4daabca814ee"
branch_labels = None
depends_on = None


def upgrade():
    modifications = [
        migration_utils.ColumnTransformation("text", [sa.Text()], {"nullable": True})
    ]
    migration_utils.modify_columns("template", modifications)


def downgrade():
    modifications = [
        migration_utils.ColumnTransformation(
            "text", [sa.String(length=255)], {"nullable": True}
        )
    ]
    migration_utils.modify_columns("template", modifications)
