"""drop_unused_tables

Nothing ever read or wrote `agent_config_versions` or `experiments`; the models were removed from the code.

Revision ID: b7c41e5a9d20
Revises: 19ddfd7201e0
Create Date: 2026-11-11 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b7c41e5a9d20'
down_revision: str | Sequence[str] | None = '19ddfd7201e0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the two unused tables (and their indexes)."""
    op.drop_index(op.f('ix_experiments_name'), table_name='experiments')
    op.drop_index(op.f('ix_experiments_call_id'), table_name='experiments')
    op.drop_table('experiments')
    op.drop_table('agent_config_versions')


def downgrade() -> None:
    """Recreate the two tables exactly as the initial schema defined them (empty)."""
    op.create_table('agent_config_versions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('version_hash', sa.String(length=64), nullable=False),
    sa.Column('persona', sa.String(length=32), nullable=False),
    sa.Column('config_yaml', sa.Text(), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('version_hash')
    )
    op.create_table('experiments',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('variant', sa.String(length=32), nullable=False),
    sa.Column('call_id', sa.String(length=64), nullable=False),
    sa.Column('metric_name', sa.String(length=64), nullable=False),
    sa.Column('metric_value', sa.Float(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['call_id'], ['calls.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_experiments_call_id'), 'experiments', ['call_id'], unique=False)
    op.create_index(op.f('ix_experiments_name'), 'experiments', ['name'], unique=False)
