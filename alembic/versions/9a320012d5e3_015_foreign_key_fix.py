"""015 foreign key fix

Revision ID: 9a320012d5e3
Revises: 9a315012d5e3
Create Date: 2017-07-10 19:46:00.760309

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9a320012d5e3'
down_revision = '9a315012d5e3'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(u'subdom_watch_entry_watch_id', 'subdomain_watch_result_entry', type_='foreignkey')
    op.create_foreign_key('subdom_watch_entry_watch_id', 'subdomain_watch_result_entry', 'subdomain_watch_target', ['watch_id'], ['id'], ondelete='CASCADE')
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint('subdom_watch_entry_watch_id', 'subdomain_watch_result_entry', type_='foreignkey')
    op.create_foreign_key(u'subdom_watch_entry_watch_id', 'subdomain_watch_result_entry', 'watch_target', ['watch_id'], ['id'], ondelete=u'CASCADE')
    # ### end Alembic commands ###
