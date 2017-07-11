"""014 initial blacklist

Revision ID: 13a9bc619477
Revises: 6da1d6465377
Create Date: 2017-07-10 17:11:20.747020

"""
from alembic import op
from alembic import context

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.dialects.mysql import INTEGER
from sqlalchemy import event, UniqueConstraint, orm
from sqlalchemy import Column, DateTime, String, Integer, ForeignKey, func, BLOB, Text, BigInteger, SmallInteger
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session as BaseSession, relationship, scoped_session

import logging
import sys
import pkg_resources
from keychest import util
from keychest import util_cert
from keychest.tls_domain_tools import TlsDomainTools
from keychest.dbutil import DbHelper, ResultModelUpdater
from keychest.consts import BlacklistRuleType


# revision identifiers, used by Alembic.
revision = '13a9bc619477'
down_revision = '6da1d6465377'
branch_labels = None
depends_on = None


Base = declarative_base()
logger = logging.getLogger(__name__)


#
# Base classes for data migration
#


class DbSubdomainScanBlacklist(Base):
    """
    Blacklist for subdomain scanning
    Excluding too popular services not to overhelm scanning engine just by trying it on google, facebook, ...
    """
    __tablename__ = 'subdomain_scan_blacklist'
    id = Column(BigInteger, primary_key=True)
    rule = Column(String(255), nullable=False)  # usually domain suffix to match
    rule_type = Column(SmallInteger, default=0)  # suffix / exact / regex match

    detection_code = Column(SmallInteger, default=0)  # for auto-detection
    detection_value = Column(Integer, default=0)  # auto-detection threshold, e.g., 5000 certificates
    detection_first_at = Column(DateTime, default=None)  # first auto-detection
    detection_last_at = Column(DateTime, default=None)  # last auto-detection
    detection_num = Column(Integer, default=0)  # number of auto-detection triggers

    created_at = Column(DateTime, default=None)
    updated_at = Column(DateTime, default=func.now())


def upgrade():
    if context.is_offline_mode():
        logger.warning('Data migration skipped in the offline mode')
        return

    resource_package = 'keychest'  # Could be any module/package name
    resource_path = '/'.join(('data', 'blacklist.txt'))
    template = pkg_resources.resource_string(resource_package, resource_path)
    if util.is_empty(template):
        raise ValueError('Blacklist is empty')

    bind = op.get_bind()
    sess = scoped_session(sessionmaker(bind=bind))

    # blacklist processing
    domains = util.stable_uniq(util.compact([x.strip().lower() for x in template.split('\n')]))

    sess.query(DbSubdomainScanBlacklist).delete()
    sess.commit()

    for domain in domains:
        entry = DbSubdomainScanBlacklist()
        entry.created_at = sa.func.now()
        entry.updated_at = sa.func.now()
        entry.rule_type = BlacklistRuleType.MATCH
        entry.rule = domain
        sess.add(entry)

    sess.commit()


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    pass
    # ### end Alembic commands ###