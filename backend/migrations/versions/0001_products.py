import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "products",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("image_key", sa.String(160), nullable=False),
        sa.Column("telegram_update_id", sa.BigInteger(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("telegram_update_id", name="uq_products_telegram_update_id"),
        sa.CheckConstraint("length(trim(title)) BETWEEN 1 AND 200", name="ck_products_title"),
        sa.CheckConstraint("telegram_update_id >= 0", name="ck_products_update_id"),
    )


def downgrade():
    op.drop_table("products")
