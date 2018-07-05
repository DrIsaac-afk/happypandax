"""new version

Revision ID: 5c54a1992a70
Revises: 51a20ca3c05f
Create Date: 2018-07-05 18:58:06.457194

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5c54a1992a70'
down_revision = None
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_%s" % engine_name]()


def downgrade(engine_name):
    globals()["downgrade_%s" % engine_name]()





def upgrade_dbengine():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('filter', 'regex')
    # ### end Alembic commands ###


def downgrade_dbengine():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('filter', sa.Column('regex', sa.BOOLEAN(), nullable=False))
    # ### end Alembic commands ###

