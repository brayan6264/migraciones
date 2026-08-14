"""add skip_existing to migration_batches

Revision ID: b5d1c2e3f4a5
Revises: a3c7e0f1b902
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5d1c2e3f4a5'
down_revision: Union[str, None] = 'a3c7e0f1b902'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'migration_batches',
        sa.Column('skip_existing', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('migration_batches', 'skip_existing')
