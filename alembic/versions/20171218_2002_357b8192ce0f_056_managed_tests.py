"""056 managed tests

Revision ID: 357b8192ce0f
Revises: 0b3facb2f325
Create Date: 2017-12-18 20:02:17.636564+00:00

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = '357b8192ce0f'
down_revision = '0b3facb2f325'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('managed_test_profiles',
                    sa.Column('id', sa.BigInteger(), nullable=False),
                    sa.Column('cert_renew_check_data', sa.Text(), nullable=True),
                    sa.Column('cert_renew_check_strategy', sa.String(length=255), nullable=True),
                    sa.Column('scan_key', sa.String(length=255), nullable=True),
                    sa.Column('scan_passive', sa.SmallInteger, nullable=False, server_default='0'),
                    sa.Column('scan_scheme', sa.String(length=255), nullable=True),
                    sa.Column('scan_port', sa.String(length=255), nullable=True),
                    sa.Column('scan_connect', sa.SmallInteger(), nullable=False),
                    sa.Column('scan_data', sa.Text(), nullable=True),
                    sa.Column('scan_service_id', sa.BigInteger(), nullable=True),
                    sa.Column('top_domain_id', sa.BigInteger(), nullable=True),
                    sa.Column('created_at', sa.DateTime(), nullable=True),
                    sa.Column('updated_at', sa.DateTime(), nullable=True),
                    sa.Column('deleted_at', sa.DateTime(), nullable=True),
                    sa.ForeignKeyConstraint(['scan_service_id'], ['watch_service.id'],
                                            name='fk_managed_test_profiles_scan_service_id', ondelete='SET NULL'),
                    sa.ForeignKeyConstraint(['top_domain_id'], ['base_domain.id'],
                                            name='fk_managed_test_profiles_base_domain_id', ondelete='SET NULL'),
                    sa.PrimaryKeyConstraint('id')
                    )
    op.create_index(op.f('ix_managed_test_profiles_scan_service_id'), 'managed_test_profiles', ['scan_service_id'], unique=False)
    op.create_index(op.f('ix_managed_test_profiles_top_domain_id'), 'managed_test_profiles', ['top_domain_id'], unique=False)

    op.create_table('managed_tests',
                    sa.Column('id', sa.BigInteger(), nullable=False),
                    sa.Column('solution_id', sa.BigInteger(), nullable=False),
                    sa.Column('service_id', sa.BigInteger(), nullable=False),
                    sa.Column('host_id', sa.BigInteger(), nullable=True),
                    sa.Column('scan_data', sa.Text(), nullable=True),
                    sa.Column('last_scan_at', sa.DateTime(), nullable=True),
                    sa.Column('last_scan_status', sa.SmallInteger(), nullable=True),
                    sa.Column('last_scan_data', sa.Text(), nullable=True),
                    sa.Column('created_at', sa.DateTime(), nullable=True),
                    sa.Column('updated_at', sa.DateTime(), nullable=True),
                    sa.Column('deleted_at', sa.DateTime(), nullable=True),
                    sa.ForeignKeyConstraint(['host_id'], ['managed_hosts.id'], name='fk_managed_tests_managed_host_id',
                                            ondelete='CASCADE'),
                    sa.ForeignKeyConstraint(['service_id'], ['managed_services.id'],
                                            name='fk_managed_tests_managed_service_id', ondelete='CASCADE'),
                    sa.ForeignKeyConstraint(['solution_id'], ['managed_solutions.id'],
                                            name='fk_managed_tests_managed_solution_id', ondelete='CASCADE'),
                    sa.PrimaryKeyConstraint('id')
                    )
    op.create_index(op.f('ix_managed_tests_host_id'), 'managed_tests', ['host_id'], unique=False)
    op.create_index(op.f('ix_managed_tests_service_id'), 'managed_tests', ['service_id'], unique=False)
    op.create_index(op.f('ix_managed_tests_solution_id'), 'managed_tests', ['solution_id'], unique=False)

    op.create_table('managed_certificates',
                    sa.Column('id', sa.BigInteger(), nullable=False),
                    sa.Column('solution_id', sa.BigInteger(), nullable=False),
                    sa.Column('service_id', sa.BigInteger(), nullable=False),
                    sa.Column('certificate_key', sa.String(length=255), nullable=True),
                    sa.Column('certificate_id', sa.BigInteger(), nullable=True),
                    sa.Column('deprecated_certificate_id', sa.BigInteger(), nullable=True),
                    sa.Column('cert_params', sa.Text(), nullable=True),
                    sa.Column('record_deprecated_at', sa.DateTime(), nullable=True),
                    sa.Column('last_check_at', sa.DateTime(), nullable=True),
                    sa.Column('last_check_status', sa.SmallInteger(), nullable=True),
                    sa.Column('last_check_data', sa.Text(), nullable=True),
                    sa.Column('created_at', sa.DateTime(), nullable=True),
                    sa.Column('updated_at', sa.DateTime(), nullable=True),
                    sa.ForeignKeyConstraint(['certificate_id'], ['certificates.id'],
                                            name='fk_managed_certificates_certificate_id', ondelete='SET NULL'),
                    sa.ForeignKeyConstraint(['deprecated_certificate_id'], ['certificates.id'],
                                            name='fk_managed_certificates_deprecated_certificate_id',
                                            ondelete='SET NULL'),
                    sa.ForeignKeyConstraint(['service_id'], ['managed_services.id'],
                                            name='fk_managed_certificate_service_id', ondelete='CASCADE'),
                    sa.ForeignKeyConstraint(['solution_id'], ['managed_solutions.id'],
                                            name='fk_managed_certificate_managed_solution_id', ondelete='CASCADE'),
                    sa.PrimaryKeyConstraint('id')
                    )
    op.create_index(op.f('ix_managed_certificates_certificate_id'), 'managed_certificates', ['certificate_id'],
                    unique=False)
    op.create_index(op.f('ix_managed_certificates_deprecated_certificate_id'), 'managed_certificates',
                    ['deprecated_certificate_id'], unique=False)
    op.create_index(op.f('ix_managed_certificates_service_id'), 'managed_certificates', ['service_id'], unique=False)
    op.create_index(op.f('ix_managed_certificates_solution_id'), 'managed_certificates', ['solution_id'], unique=False)

    op.create_table('managed_cert_issue',
                    sa.Column('id', sa.BigInteger(), nullable=False),
                    sa.Column('solution_id', sa.BigInteger(), nullable=False),
                    sa.Column('service_id', sa.BigInteger(), nullable=False),
                    sa.Column('certificate_id', sa.BigInteger(), nullable=True),
                    sa.Column('new_certificate_id', sa.BigInteger(), nullable=True),
                    sa.Column('affected_certs_ids', sa.Text(), nullable=True),
                    sa.Column('request_data', sa.Text(), nullable=True),
                    sa.Column('last_issue_at', sa.DateTime(), nullable=True),
                    sa.Column('last_issue_status', sa.SmallInteger(), nullable=True),
                    sa.Column('last_issue_data', sa.Text(), nullable=True),
                    sa.Column('created_at', sa.DateTime(), nullable=True),
                    sa.Column('updated_at', sa.DateTime(), nullable=True),
                    sa.ForeignKeyConstraint(['certificate_id'], ['certificates.id'],
                                            name='fk_managed_cert_issue_certificate_id', ondelete='SET NULL'),
                    sa.ForeignKeyConstraint(['new_certificate_id'], ['certificates.id'],
                                            name='fk_managed_cert_issue_new_certificate_id', ondelete='SET NULL'),
                    sa.ForeignKeyConstraint(['solution_id'], ['managed_solutions.id'], name='fk_managed_cert_issue_managed_solution_id',
                                            ondelete='CASCADE'),
                    sa.ForeignKeyConstraint(['service_id'], ['managed_services.id'], name='fk_managed_cert_issue_service_id',
                                            ondelete='CASCADE'),
                    sa.PrimaryKeyConstraint('id')
                    )
    op.create_index(op.f('ix_managed_cert_issue_certificate_id'), 'managed_cert_issue', ['certificate_id'],
                    unique=False)
    op.create_index(op.f('ix_managed_cert_issue_new_certificate_id'), 'managed_cert_issue', ['new_certificate_id'],
                    unique=False)
    op.create_index(op.f('ix_managed_cert_issue_solution_id'), 'managed_cert_issue', ['solution_id'], unique=False)
    op.create_index(op.f('ix_managed_cert_issue_service_id'), 'managed_cert_issue', ['service_id'], unique=False)

    op.add_column('managed_services', sa.Column('svc_watch_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_managed_services_svc_watch_id'), 'managed_services', ['svc_watch_id'], unique=False)
    op.create_foreign_key('managed_services_svc_watch_id', 'managed_services', 'watch_target', ['svc_watch_id'], ['id'],
                          ondelete='SET NULL')
    op.create_unique_constraint('uk_managed_solution_to_service_sol_svc', 'managed_solution_to_service',
                                ['service_id', 'solution_id'])
    op.drop_index('uk_managed_solution_to_service_svc_sol', table_name='managed_solution_to_service')
    op.drop_column('managed_solutions', 'sol_data')
    op.drop_column('managed_solutions', 'sol_desc')
    op.add_column('scan_handshakes', sa.Column('test_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_scan_handshakes_test_id'), 'scan_handshakes', ['test_id'], unique=False)
    op.create_foreign_key('tls_watch_managed_tests_id', 'scan_handshakes', 'managed_tests', ['test_id'], ['id'],
                          ondelete='SET NULL')

    op.add_column('managed_services', sa.Column('test_profile_id', sa.BigInteger(), nullable=True))
    op.create_index(op.f('ix_managed_services_test_profile_id'), 'managed_services', ['test_profile_id'], unique=False)
    op.create_foreign_key('managed_services_test_profile_id', 'managed_services', 'managed_test_profiles',
                          ['test_profile_id'], ['id'], ondelete='SET NULL')

    op.alter_column('managed_services', 'owner_id',
                    existing_type=mysql.BIGINT(display_width=20),
                    nullable=False)
    op.alter_column('managed_solutions', 'owner_id',
                    existing_type=mysql.BIGINT(display_width=20),
                    nullable=False)

    op.add_column('managed_services', sa.Column('svc_aux_names', sa.Text(), nullable=True))
    op.add_column('managed_services', sa.Column('svc_ca', sa.String(length=255), nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column('managed_solutions', 'owner_id',
                    existing_type=mysql.BIGINT(display_width=20),
                    nullable=True)
    op.alter_column('managed_services', 'owner_id',
                    existing_type=mysql.BIGINT(display_width=20),
                    nullable=True)

    op.drop_constraint('managed_services_test_profile_id', 'managed_services', type_='foreignkey')
    op.drop_column('managed_services', 'test_profile_id')

    op.drop_constraint('tls_watch_managed_tests_id', 'scan_handshakes', type_='foreignkey')
    # op.drop_index(op.f('ix_scan_handshakes_test_id'), table_name='scan_handshakes')
    op.drop_column('scan_handshakes', 'test_id')
    op.add_column('managed_solutions', sa.Column('sol_desc', mysql.TEXT(), nullable=True))
    op.add_column('managed_solutions', sa.Column('sol_data', mysql.TEXT(), nullable=True))
    op.create_index('uk_managed_solution_to_service_svc_sol', 'managed_solution_to_service',
                    ['solution_id', 'service_id'], unique=True)
    op.drop_constraint('uk_managed_solution_to_service_sol_svc', 'managed_solution_to_service', type_='unique')
    op.drop_constraint('managed_services_svc_watch_id', 'managed_services', type_='foreignkey')
    # op.drop_index(op.f('ix_managed_services_svc_watch_id'), table_name='managed_services')
    op.drop_column('managed_services', 'svc_watch_id')
    # op.drop_index(op.f('ix_managed_cert_issue_test_id'), table_name='managed_cert_issue')
    # op.drop_index(op.f('ix_managed_cert_issue_new_certificate_id'), table_name='managed_cert_issue')
    # op.drop_index(op.f('ix_managed_cert_issue_certificate_id'), table_name='managed_cert_issue')
    op.drop_table('managed_cert_issue')
    # op.drop_index(op.f('ix_managed_tests_watch_target_id'), table_name='managed_tests')
    # op.drop_index(op.f('ix_managed_tests_top_domain_id'), table_name='managed_tests')
    # op.drop_index(op.f('ix_managed_tests_solution_id'), table_name='managed_tests')
    # op.drop_index(op.f('ix_managed_tests_service_id'), table_name='managed_tests')
    # op.drop_index(op.f('ix_managed_tests_scan_service_id'), table_name='managed_tests')
    # op.drop_index(op.f('ix_managed_tests_host_id'), table_name='managed_tests')
    op.drop_table('managed_tests')
    op.drop_table('managed_certificates')
    op.drop_table('managed_test_profiles')

    op.drop_column('managed_services', 'svc_ca')
    op.drop_column('managed_services', 'svc_aux_names')
    # ### end Alembic commands ###
