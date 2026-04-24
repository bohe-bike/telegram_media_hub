"""Initial schema

Revision ID: 001
Revises: 
Create Date: 2026-04-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- tasks table ---
    op.create_table('tasks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source_type', sa.Enum('tg_video', 'tg_document', 'tg_photo', 'tg_audio', 'external_link', name='sourcetype'), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('telegram_file_id', sa.String(length=255), nullable=True),
        sa.Column('telegram_chat_id', sa.BigInteger(), nullable=True),
        sa.Column('telegram_message_id', sa.Integer(), nullable=True),
        sa.Column('file_name', sa.String(length=512), nullable=True),
        sa.Column('status', sa.Enum('pending', 'downloading', 'completed', 'failed', 'retrying', 'cancelled', name='taskstatus'), nullable=False, server_default='pending'),
        sa.Column('retry_count', sa.Integer(), nullable=True),
        sa.Column('max_retries', sa.Integer(), nullable=True),
        sa.Column('proxy_used', sa.String(length=255), nullable=True),
        sa.Column('speed', sa.Float(), nullable=True),
        sa.Column('file_size', sa.BigInteger(), nullable=True),
        sa.Column('downloaded_size', sa.BigInteger(), nullable=True),
        sa.Column('local_path', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tasks_status'), 'tasks', ['status'], unique=False)
    op.create_index(op.f('ix_tasks_source_type'), 'tasks', ['source_type'], unique=False)

    # --- proxies table ---
    op.create_table('proxies',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('proxy_url', sa.String(length=255), nullable=False),
        sa.Column('status', sa.Enum('active', 'failed', 'disabled', name='proxystatus'), nullable=False, server_default='active'),
        sa.Column('latency', sa.Float(), nullable=True),
        sa.Column('fail_count', sa.Integer(), nullable=True),
        sa.Column('last_check_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('proxy_url')
    )


def downgrade() -> None:
    op.drop_table('proxies')
    op.drop_table('tasks')
    
    # Drop enums
    op.execute("DROP TYPE IF EXISTS sourcetype")
    op.execute("DROP TYPE IF EXISTS taskstatus")
    op.execute("DROP TYPE IF EXISTS proxystatus")
