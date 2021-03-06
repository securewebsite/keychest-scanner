"""010 denormalized optimization

Revision ID: 9a290012d5e3
Revises: 9a285012d5e3
Create Date: 2017-07-09 20:55:22.918622

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a290012d5e3'
down_revision = '9a285012d5e3'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('scan_dns', sa.Column('num_ipv4', sa.SmallInteger(), nullable=False, server_default='0'))
    op.add_column('scan_dns', sa.Column('num_ipv6', sa.SmallInteger(), nullable=False, server_default='0'))
    op.add_column('scan_dns', sa.Column('num_res', sa.SmallInteger(), nullable=False, server_default='0'))
    op.add_column('watch_target', sa.Column('last_dns_scan_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_watch_target_last_dns_scan_id'), 'watch_target', ['last_dns_scan_id'], unique=False)
    op.create_foreign_key('wt_scan_dns_id', 'watch_target', 'scan_dns', ['last_dns_scan_id'], ['id'], ondelete='SET NULL')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint('wt_scan_dns_id', 'watch_target', type_='foreignkey')
    op.drop_index(op.f('ix_watch_target_last_dns_scan_id'), table_name='watch_target')
    op.drop_column('watch_target', 'last_dns_scan_id')
    op.drop_column('scan_dns', 'num_res')
    op.drop_column('scan_dns', 'num_ipv6')
    op.drop_column('scan_dns', 'num_ipv4')
    # ### end Alembic commands ###
