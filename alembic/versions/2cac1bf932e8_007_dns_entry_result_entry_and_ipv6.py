"""007 dns entry and ipv6 

Revision ID: 2cac1bf932e8
Revises: dec318e2036c
Create Date: 2017-07-09 17:32:39.987794

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '2cac1bf932e8'
down_revision = 'dec318e2036c'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('subdomain_watch_result_entry',
                    sa.Column('id', sa.BigInteger(), nullable=False),
                    sa.Column('watch_id', sa.BigInteger(), nullable=False),
                    sa.Column('is_wildcard', sa.SmallInteger(), nullable=False),
                    sa.Column('is_internal', sa.SmallInteger(), nullable=False),
                    sa.Column('ip', sa.String(length=191), nullable=False),
                    sa.Column('res_order', sa.SmallInteger(), nullable=False),
                    sa.Column('created_at', sa.DateTime(), nullable=True),
                    sa.Column('updated_at', sa.DateTime(), nullable=True),
                    sa.Column('last_scan_at', sa.DateTime(), nullable=True),
                    sa.Column('num_scans', sa.Integer(), nullable=False),
                    sa.Column('last_scan_id', sa.BigInteger(), nullable=True),
                    sa.Column('first_scan_id', sa.BigInteger(), nullable=True),
                    sa.ForeignKeyConstraint(['first_scan_id'], ['subdomain_results.id'],
                                            name='subdom_watch_entry_subdomain_results_id_last', ondelete='SET NULL'),
                    sa.ForeignKeyConstraint(['last_scan_id'], ['subdomain_results.id'],
                                            name='subdom_watch_entry_subdomain_results_id_first', ondelete='SET NULL'),
                    sa.ForeignKeyConstraint(['watch_id'], ['watch_target.id'], name='subdom_watch_entry_watch_id',
                                            ondelete='CASCADE'),
                    sa.PrimaryKeyConstraint('id')
                    )
    op.create_index(op.f('ix_subdomain_watch_result_entry_first_scan_id'), 'subdomain_watch_result_entry',
                    ['first_scan_id'], unique=False)
    op.create_index(op.f('ix_subdomain_watch_result_entry_last_scan_id'), 'subdomain_watch_result_entry',
                    ['last_scan_id'], unique=False)
    op.create_index(op.f('ix_subdomain_watch_result_entry_watch_id'), 'subdomain_watch_result_entry', ['watch_id'],
                    unique=False)

    op.create_table('scan_dns_entry',
                    sa.Column('id', sa.BigInteger(), nullable=False),
                    sa.Column('scan_id', sa.BigInteger(), nullable=False),
                    sa.Column('is_ipv6', sa.SmallInteger(), nullable=False),
                    sa.Column('is_internal', sa.SmallInteger(), nullable=False),
                    sa.Column('ip', sa.String(length=191), nullable=False),
                    sa.Column('res_order', sa.SmallInteger(), nullable=False),
                    sa.ForeignKeyConstraint(['scan_id'], ['scan_dns.id'], name='scan_dns_entry_scan_id',
                                            ondelete='CASCADE'),
                    sa.PrimaryKeyConstraint('id')
                    )
    op.create_index(op.f('ix_scan_dns_entry_scan_id'), 'scan_dns_entry', ['scan_id'], unique=False)

    op.add_column(u'scan_handshakes', sa.Column('is_ipv6', sa.SmallInteger(), nullable=False))

    op.alter_column(u'user_subdomain_watch_target', 'auto_fill_watches',
                    existing_type=mysql.SMALLINT(display_width=6),
                    nullable=False)

    op.drop_column(u'watch_target', 'scan_periodicity')
    op.drop_column(u'watch_target', 'user_id')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(u'watch_target',
                  sa.Column('user_id', mysql.BIGINT(display_width=20), autoincrement=False, nullable=True))
    op.add_column(u'watch_target',
                  sa.Column('scan_periodicity', mysql.BIGINT(display_width=20), autoincrement=False, nullable=True))
    op.alter_column(u'user_subdomain_watch_target', 'auto_fill_watches',
                    existing_type=mysql.SMALLINT(display_width=6),
                    nullable=True)
    op.drop_column(u'scan_handshakes', 'is_ipv6')
    op.drop_index(op.f('ix_scan_dns_entry_scan_id'), table_name='scan_dns_entry')
    op.drop_table('scan_dns_entry')
    op.drop_index(op.f('ix_subdomain_watch_result_entry_watch_id'), table_name='subdomain_watch_result_entry')
    op.drop_index(op.f('ix_subdomain_watch_result_entry_last_scan_id'), table_name='subdomain_watch_result_entry')
    op.drop_index(op.f('ix_subdomain_watch_result_entry_first_scan_id'), table_name='subdomain_watch_result_entry')
    op.drop_table('subdomain_watch_result_entry')
    # ### end Alembic commands ###
