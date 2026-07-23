"""add destination_base_path to migration_batches

Revision ID: a3c7e0f1b902
Revises: 1f66bdffd67a
Create Date: 2026-07-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c7e0f1b902'
down_revision: Union[str, None] = '1f66bdffd67a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('migration_batches', sa.Column('destination_base_path', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('migration_batches', 'destination_base_path')
