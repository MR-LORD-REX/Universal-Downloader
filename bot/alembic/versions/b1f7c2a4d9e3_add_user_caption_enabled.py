"""add users.caption_enabled

Revision ID: b1f7c2a4d9e3
Revises: 8cb93939982a
Create Date: 2026-09-24 20:40:12.118004

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1f7c2a4d9e3'
down_revision: Union[str, None] = '8cb93939982a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing rows keep the previous behaviour (full captions).
    op.add_column(
        'users',
        sa.Column(
            'caption_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('caption_enabled')