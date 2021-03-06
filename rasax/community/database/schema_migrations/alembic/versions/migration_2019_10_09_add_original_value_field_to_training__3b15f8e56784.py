"""Add `original_value` field to training data entities.

Reason:
Add a new field (`original_value`) to TrainingDataEntity which always contains
the original text value of the entity, even if it has been defined as a synonym
of something else.

This new field is specifically needed for entities which have been defined as a
synonym of something else using the (<value>)[<entity>:<synonym>] notation in
the Markdown NLU examples format. In these cases, the original value (<value>)
was not being stored in the database.

The original value is needed in order to count how many times a particular entity
value is used in the NLU training data. The column is indexed in order to allow
for fast lookups when calculating these numbers.

Revision ID: 3b15f8e56784
Revises: d45a0bf21e89

"""
from typing import Text

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine import RowProxy
from rasax.community.database.schema_migrations.alembic import utils as migration_utils

# revision identifiers, used by Alembic.
revision = "3b15f8e56784"
down_revision = "d45a0bf21e89"
branch_labels = None
depends_on = None

INDEX_NAME = "nlu_train_dat_ent_orig_val_idx"


def upgrade():
    def _get_original_value(training_data_entity_row: RowProxy) -> Text:
        return training_data_entity_row.value[
            training_data_entity_row.start : training_data_entity_row.end
        ]

    migration_utils.modify_columns(
        "nlu_training_data_entity",
        [
            migration_utils.ColumnTransformation(
                "value",
                [sa.String(255)],
                modify_from_row=_get_original_value,
                new_column_name="original_value",
            )
        ],
    )

    with op.batch_alter_table("nlu_training_data_entity") as batch_op:
        batch_op.create_index(INDEX_NAME, ["original_value"], unique=False)


def downgrade():
    with op.batch_alter_table("nlu_training_data_entity") as batch_op:
        batch_op.drop_index(INDEX_NAME)
        batch_op.drop_column("original_value")
