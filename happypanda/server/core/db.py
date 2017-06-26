from sqlalchemy.engine import Engine
from sqlalchemy import String as _String
from sqlalchemy.inspection import inspect
from sqlalchemy.ext.mutable import Mutable
from sqlalchemy.ext.declarative import declarative_base, declared_attr
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import BinaryExpression, func, literal
from sqlalchemy.sql.operators import custom_op
from sqlalchemy.orm import (
    sessionmaker,
    relationship,
    validates,
    object_session,
    scoped_session,
    attributes,
    state,
    collections,
    dynamic)
from sqlalchemy import (
    create_engine,
    event,
    exc,
    and_,
    or_,
    Boolean,
    Column,
    Integer,
    ForeignKey,
    Table,
    UniqueConstraint,
    Float,
    Enum,
    TypeDecorator,
    text)
from sqlalchemy_utils import (
    ArrowType,
    generic_repr,
    force_instant_defaults,
    force_auto_coercion)

import arrow
import os
import enum
import re
import bcrypt

from happypanda.common import constants, exceptions, hlogger

force_instant_defaults()
force_auto_coercion()

log = hlogger.Logger(__name__)

and_op = and_
or_op = or_
sa_text = text


class String(_String):
    """Enchanced version of standard SQLAlchemy's :class:`String`.
    Supports additional operators that can be used while constructing
    filter expressions.
    """
    class comparator_factory(_String.comparator_factory):
        """Contains implementation of :class:`String` operators
        related to regular expressions.
        """

        def regexp(self, other):
            return RegexMatchExpression(
                self.expr, literal(other), custom_op('~'))

        def iregexp(self, other):
            return RegexMatchExpression(
                self.expr, literal(other), custom_op('~*'))

        def not_regexp(self, other):
            return RegexMatchExpression(
                self.expr, literal(other), custom_op('!~'))

        def not_iregexp(self, other):
            return RegexMatchExpression(
                self.expr, literal(other), custom_op('!~*'))


class RegexMatchExpression(BinaryExpression):
    """Represents matching of a column againsts a regular expression."""


SQLITE_REGEX_FUNCTIONS = {
    '~': ('REGEXP',
          lambda value, regex: bool(re.match(regex, value))),
    '~*': ('IREGEXP',
           lambda value, regex: bool(re.match(regex, value, re.IGNORECASE))),
    '!~': ('NOT_REGEXP',
           lambda value, regex: not re.match(regex, value)),
    '!~*': ('NOT_IREGEXP',
            lambda value, regex: not re.match(regex, value, re.IGNORECASE)),
}


class PasswordHash(Mutable):
    def __init__(self, hash_, rounds=None):
        assert len(hash_) == 60, 'bcrypt hash should be 60 chars.'
        assert hash_.count('$'), 'bcrypt hash should have 3x "$".'
        self.hash = str(hash_)
        self.rounds = int(self.hash.split('$')[2])
        self.desired_rounds = rounds or self.rounds

    def __eq__(self, candidate):
        """Hashes the candidate string and compares it to the stored hash.

        If the current and desired number of rounds differ, the password is
        re-hashed with the desired number of rounds and updated with the results.
        This will also mark the object as having changed (and thus need updating).
        """
        if isinstance(candidate, str):
            if self.hash == bcrypt.hashpw(candidate, self.hash):
                if self.rounds < self.desired_rounds:
                    self._rehash(candidate)
                return True
        return False

    def __repr__(self):
        """Simple object representation."""
        return '<{}>'.format(type(self).__name__)

    @classmethod
    def coerce(cls, key, value):
        """Ensure that loaded values are PasswordHashes."""
        if isinstance(value, PasswordHash):
            return value
        return super(PasswordHash, cls).coerce(key, value)

    @classmethod
    def new(cls, password, rounds):
        """Returns a new PasswordHash object for the given password and rounds."""
        return cls(cls._new(password, rounds))

    @staticmethod
    def _new(password, rounds):
        """Returns a new bcrypt hash for the given password and rounds."""
        return bcrypt.hashpw(password, bcrypt.gensalt(rounds))

    def _rehash(self, password):
        """Recreates the internal hash and marks the object as changed."""
        self.hash = self._new(password, self.desired_rounds)
        self.rounds = self.desired_rounds
        self.changed()


class Password(TypeDecorator):
    """Allows storing and retrieving password hashes using PasswordHash."""
    impl = String

    def __init__(self, rounds=12, **kwds):
        self.rounds = rounds
        super().__init__(**kwds)

    def process_bind_param(self, value, dialect):
        """Ensure the value is a PasswordHash and then return its hash."""
        p = self._convert(value)
        return p.hash if p else None

    def process_result_value(self, value, dialect):
        """Convert the hash to a PasswordHash, if it's non-NULL."""
        if value is not None:
            return PasswordHash(value, rounds=self.rounds)

    def validator(self, password):
        """Provides a validator/converter for @validates usage."""
        return self._convert(password)

    def _convert(self, value):
        """Returns a PasswordHash from the given string.

        PasswordHash instances or None values will return unchanged.
        Strings will be hashed and the resulting PasswordHash returned.
        Any other input will result in a TypeError.
        """
        if isinstance(value, PasswordHash):
            return value
        elif isinstance(value, str):
            return PasswordHash.new(value, self.rounds)
        elif value is not None:
            raise TypeError(
                'Cannot convert {} to a PasswordHash'.format(type(value)))


class BaseID:
    id = Column(Integer, primary_key=True)

    def delete(self):
        sess = object_session(self)
        if not sess:
            sess = constants.db_session()
        sess.delete(self)
        return sess


class NameMixin:
    name = Column(String, nullable=False, default='', unique=True)

    def exists(self, obj=False, strict=False):
        "obj: queries for the full object and returns it"
        if not constants.db_session:
            return self
        sess = constants.db_session()
        if obj:
            if strict:
                e = sess.query(
                    self.__class__).filter_by(
                    name=self.name).scalar()
            else:
                e = sess.query(
                    self.__class__).filter(
                    self.__class__.name.ilike(
                        "%{}%".format(
                            self.name))).scalar()
            if not e:
                e = self
        else:
            if strict:
                e = sess.query(
                    self.__class__.id).filter_by(
                    name=self.name).scalar() is not None
            else:
                e = sess.query(
                    self.__class__.id).filter(
                    self.__class__.name.ilike(
                        "%{}%".format(
                            self.name))).scalar() is not None
        sess.close()
        return e

    def __repr__(self):
        return "<{}(ID: {}, Name: {})>".format(
            self.__class__.__name__, self.id, self.name)


class ProfileMixin:

    def get_profile(self, profile_type):
        return ''


Base = declarative_base(cls=BaseID)


def validate_int(value):
    if isinstance(value, str):
        try:
            value = int(value)
        except BaseException:
            raise AssertionError(
                "Column only accepts integer, not {}".format(
                    type(value)))
    else:
        assert isinstance(
            value, int) or value is None, "Column only accepts integer, not {}".format(
            type(value))
    return value


def validate_string(value):
    assert isinstance(
        value, str) or value is None, "Column only accepts string, not {}".format(
        type(value))
    return value


def validate_arrow(value):
    assert isinstance(
        value, arrow.Arrow) or value is None, "Column only accepts arrow types, not {}".format(
        type(value))
    return value


def validate_bool(value):
    assert isinstance(
        value, bool) or value is None, "Column only accepts boolean, not {}".format(
        type(value))
    return value


validators = {
    Integer: validate_int,
    String: validate_string,
    ArrowType: validate_arrow,
    Boolean: validate_bool,
}


@event.listens_for(Base, 'attribute_instrument')
def configure_listener(class_, key, inst):
    if not hasattr(inst.property, 'columns'):
        return

    @event.listens_for(inst, "set", retval=True)
    def set_(instance, value, oldvalue, initiator):
        validator = validators.get(inst.property.columns[0].type.__class__)
        if validator:
            return validator(value)
        else:
            return value


def profile_association(table_name):
    column = '{}_id'.format(table_name)
    assoc = Table(
        '{}_profiles'.format(table_name), Base.metadata, Column(
            'profile_id', Integer, ForeignKey('profile.id')), Column(
            column, Integer, ForeignKey(
                '{}.id'.format(table_name))), UniqueConstraint(
                    'profile_id', column))
    return assoc


class Life(Base):
    __tablename__ = 'life'

    version = Column(Float, nullable=False, default=constants.version_db,)
    times_opened = Column(Integer, nullable=False, default=0)

    def __repr__(self):
        return "<Version: {}, times_opened:{}>".format(
            self.version, self.times_opened)


class User(Base):
    __tablename__ = 'user'

    class Role(enum.Enum):
        admin = 'admin'
        user = 'user'
        guest = 'guest'
        default = 'default'

    name = Column(String, nullable=False, default='')
    role = Column(Enum(Role), nullable=False, default=Role.guest)
    address = Column(String, nullable=False, default='')
    context_id = Column(String, nullable=False, default='')
    password = Column(Password)

    events = relationship("Event", lazy='dynamic', back_populates='user')

    @validates('password')
    def _validate_password(self, key, password):
        return getattr(type(self), key).type.validator(password)

    @property
    def has_auth(self):
        return self.role != self.Role.guest

    @property
    def is_admin(self):
        return self.role == self.Role.admin


class Profile(Base):
    __tablename__ = 'profile'

    path = Column(String, nullable=False, default='')
    data = Column(String, nullable=False, default='')
    size = Column(String, nullable=False)
    timestamp = Column(ArrowType, nullable=False, default=arrow.now)

    def __repr__(self):
        return "Profile ID:{} Size:{} Path:{}".format(
            self.id, self.size, self.path)


class Event(Base):
    __tablename__ = 'event'

    class Action(enum.Enum):
        read = 'read'
        start = 'start'

    item_id = Column(Integer, nullable=False)
    item_name = Column(String, nullable=False)
    timestamp = Column(ArrowType, nullable=False, default=arrow.now)
    action = Column(Enum(Action), nullable=False, default=Action.read)
    user_id = Column(Integer, ForeignKey('user.id'))
    user = relationship(
        "User",
        back_populates="events",
        cascade="save-update, merge, refresh-expire")

    def __init__(
            self,
            item,
            action=Action.read,
            user_id=None,
            timestamp=arrow.now()):
        assert isinstance(item, Base)
        self.user_id = user_id
        self.item_id = item.id
        self.item_name = item.__tablename__
        self.timestamp = timestamp
        self.action = action


@generic_repr
class Hash(NameMixin, Base):
    __tablename__ = 'hash'


class NamespaceTags(Base):
    __tablename__ = 'namespace_tags'

    tag_id = Column(Integer, ForeignKey('tag.id'))
    namespace_id = Column(Integer, ForeignKey('namespace.id'))
    __table_args__ = (UniqueConstraint('tag_id', 'namespace_id'),)

    tag = relationship("Tag", cascade="save-update, merge, refresh-expire")
    namespace = relationship("Namespace",
                             cascade="save-update, merge, refresh-expire")

    def mapping_exists(self):
        sess = constants.db_session()
        e = sess.query(
            self.__class__).filter_by(
            and_(
                tag_id=self.tag_id,
                namespace_id=self.namespace_id)).scalar()
        if not e:
            e = self
        sess.close()
        return e

    def __init__(self, ns, tag):
        self.namespace = ns
        self.tag = tag


@generic_repr
class Tag(NameMixin, Base):
    __tablename__ = 'tag'

    namespaces = relationship(
        "Namespace",
        secondary='namespace_tags',
        back_populates='tags',
        lazy="dynamic")


class Namespace(NameMixin, Base):
    __tablename__ = 'namespace'

    tags = relationship(
        "Tag",
        secondary='namespace_tags',
        back_populates='namespaces',
        lazy="dynamic")


taggable_tags = Table(
    'taggable_tags', Base.metadata, Column(
        'namespace_tag_id', Integer, ForeignKey('namespace_tags.id')), Column(
            'taggable_id', Integer, ForeignKey('taggable.id')), UniqueConstraint(
                'namespace_tag_id', 'taggable_id'))


class Taggable(Base):
    __tablename__ = 'taggable'

    tags = relationship(
        "NamespaceTags",
        secondary=taggable_tags,
        lazy="dynamic")


class TaggableMixin:

    @declared_attr
    def taggable(cls):
        return relationship(
            "Taggable",
            single_parent=True,
            cascade="all, delete-orphan")

    @declared_attr
    def taggable_id(cls):
        return Column(Integer, ForeignKey('taggable.id'), nullable=False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.taggable = Taggable()

    @property
    def tags(self):
        return self.taggable.tags


gallery_artists = Table(
    'gallery_artists', Base.metadata, Column(
        'artist_id', Integer, ForeignKey('artist.id')), Column(
            'gallery_id', Integer, ForeignKey('gallery.id')), UniqueConstraint(
                'artist_id', 'gallery_id'))


@generic_repr
class Artist(NameMixin, Base):
    __tablename__ = 'artist'

    galleries = relationship(
        "Gallery",
        secondary=gallery_artists,
        back_populates='artists',
        lazy="dynamic")


gallery_circles = Table(
    'gallery_circles', Base.metadata, Column(
        'circle_id', Integer, ForeignKey(
            'circle.id',)), Column(
                'gallery_id', Integer, ForeignKey(
                    'gallery.id',)), UniqueConstraint(
                        'circle_id', 'gallery_id'))


@generic_repr
class Circle(NameMixin, Base):
    __tablename__ = 'circle'

    galleries = relationship(
        "Gallery",
        secondary=gallery_circles,
        back_populates='circles',
        lazy="dynamic")


gallery_filters = Table('gallery_filters', Base.metadata,
                        Column('filter_id', Integer, ForeignKey('filter.id')),
                        Column(
                            'gallery_id',
                            Integer,
                            ForeignKey('gallery.id')),
                        UniqueConstraint('filter_id', 'gallery_id'))

filter_profiles = profile_association("filter")


@generic_repr
class GalleryFilter(ProfileMixin, NameMixin, Base):
    __tablename__ = 'filter'
    filter = Column(String, nullable=False, default='')
    enforce = Column(Boolean, nullable=False, default=False)
    regex = Column(Boolean, nullable=False, default=False)
    l_case = Column(Boolean, nullable=False, default=False)
    strict = Column(Boolean, nullable=False, default=False)

    galleries = relationship(
        "Gallery",
        secondary=gallery_filters,
        back_populates='filters',
        lazy="dynamic")
    profiles = relationship(
        "Profile",
        secondary=filter_profiles,
        lazy='joined',
        cascade="all")


@generic_repr
class Status(NameMixin, Base):
    __tablename__ = 'status'

    groupings = relationship(
        "Grouping",
        lazy='dynamic',
        back_populates='status')


grouping_profiles = profile_association("grouping")


@generic_repr
class Grouping(ProfileMixin, NameMixin, Base):
    __tablename__ = 'grouping'
    status_id = Column(Integer, ForeignKey('status.id'))

    galleries = relationship(
        "Gallery",
        back_populates="grouping",
        lazy="dynamic",
        cascade="all, delete-orphan")
    profiles = relationship(
        "Profile",
        secondary=grouping_profiles,
        lazy='joined',
        cascade="all")
    status = relationship(
        "Status",
        back_populates="groupings",
        cascade="save-update, merge, refresh-expire")


@generic_repr
class Language(NameMixin, Base):
    __tablename__ = 'language'

    galleries = relationship(
        "Gallery",
        lazy='dynamic',
        back_populates='language')


@generic_repr
class Category(NameMixin, Base):
    __tablename__ = 'category'

    galleries = relationship(
        "Gallery",
        lazy='dynamic',
        back_populates='category')


gallery_collections = Table(
    'gallery_collections', Base.metadata, Column(
        'collection_id', Integer, ForeignKey('collection.id')), Column(
            'gallery_id', Integer, ForeignKey('gallery.id')), UniqueConstraint(
                'collection_id', 'gallery_id'))

collection_profiles = profile_association("collection")


@generic_repr
class Collection(ProfileMixin, Base):
    __tablename__ = 'collection'

    title = Column(String, nullable=False, default='')
    info = Column(String, nullable=False, default='')
    pub_date = Column(ArrowType)

    galleries = relationship(
        "Gallery",
        secondary=gallery_collections,
        back_populates="collections",
        lazy="dynamic",
        cascade="save-update, merge, refresh-expire")
    profiles = relationship(
        "Profile",
        secondary=collection_profiles,
        lazy='joined',
        cascade="all")


gallery_profiles = profile_association("gallery")


@generic_repr
class Gallery(TaggableMixin, ProfileMixin, Base):
    __tablename__ = 'gallery'

    last_read = Column(ArrowType)
    pub_date = Column(ArrowType)
    inbox = Column(Boolean, default=False)
    fav = Column(Boolean, default=False)
    info = Column(String, nullable=False, default='')
    complete_pages = Column(Boolean, default=True)
    path = Column(String, nullable=False, default='')
    fetched = Column(Boolean, default=False)
    path_in_archive = Column(String, nullable=False, default='')
    rating = Column(Integer, nullable=False, default=0)
    times_read = Column(Integer, nullable=False, default=0)
    timestamp = Column(ArrowType, nullable=False, default=arrow.now)
    number = Column(Integer, nullable=False, default=0)
    in_archive = Column(Boolean, default=False)
    category_id = Column(Integer, ForeignKey('category.id'))
    language_id = Column(Integer, ForeignKey('language.id'))
    grouping_id = Column(Integer, ForeignKey('grouping.id'))

    grouping = relationship(
        "Grouping",
        back_populates="galleries",
        cascade="save-update, merge, refresh-expire")
    collections = relationship(
        "Collection",
        secondary=gallery_collections,
        back_populates="galleries",
        cascade="save-update, merge, refresh-expire",
        lazy="dynamic")
    urls = relationship(
        "GalleryUrl",
        back_populates="gallery",
        lazy='joined',
        cascade="all,delete-orphan")
    language = relationship(
        "Language",
        back_populates="galleries",
        cascade="save-update, merge, refresh-expire")
    category = relationship(
        "Category",
        back_populates="galleries",
        cascade="save-update, merge, refresh-expire")
    circles = relationship(
        "Circle",
        secondary=gallery_circles,
        back_populates='galleries',
        lazy="joined",
        cascade="save-update, merge, refresh-expire")
    artists = relationship(
        "Artist",
        secondary=gallery_artists,
        back_populates='galleries',
        lazy="joined",
        cascade="save-update, merge, refresh-expire")
    filters = relationship(
        "GalleryFilter",
        secondary=gallery_filters,
        back_populates='galleries',
        lazy="dynamic")
    pages = relationship(
        "Page",
        back_populates="gallery",
        lazy='dynamic',
        cascade="all,delete-orphan")
    titles = relationship(
        "Title",
        back_populates="gallery",
        lazy='joined',
        cascade="all,delete-orphan")
    profiles = relationship(
        "Profile",
        secondary=gallery_profiles,
        lazy='joined',
        cascade="all")

    def read(self, user_id, datetime=None):
        "Creates a read event for user"
        if not datetime:
            datetime = arrow.now()
        self.last_read = datetime
        sess = object_session(self)
        if sess:
            sess.add(Event(self, Event.Action.read, user_id, datetime))
        else:
            log.w(
                "Cannot add gallery read event because no session exists for this object")
        self.times_read += 1
        self.last_read = datetime

    @property
    def title(self):
        "Returns default title"
        if not hasattr(self, '_title'):
            self._set_default_title()
        return self._title

    @title.setter
    def title(self, t):
        assert isinstance(t, str)
        if not hasattr(self, '_title'):
            self._set_default_title()

        if self._title:
            self._title.name = t

    def _set_default_title(self):
        if len(self.titles) == 1:
            self._title = self.titles[0]
        else:
            self._title = None

    @property
    def file_type(self):
        ""
        if not hasattr(self, '_file_type'):
            _, self._file_type = os.path.splitext(self.path)
            if not self._file_type:
                self._file_type = 'folder'
        return self._file_type

    def exists(self, obj=False, strict=False):
        """Checks if gallery exists by path
        Params:
            obj -- queries for the full object and returns it
        """
        e = self
        if not constants.db_session:
            return e
        g = self.__class__
        if self.path:
            head, tail = os.path.split(self.path)
            p, ext = os.path.splitext(tail if tail else head)
            sess = constants.db_session()
            if self.in_archive:
                head, tail = os.path.split(self.path_in_archive)
                p_a = tail if tail else head
                e = sess.query(
                    self.__class__.id).filter(
                    and_(
                        g.path.ilike(
                            "%{}%".format(p)), g.path_in_archive.ilike(
                            "%{}%".format(p_a)))).scalar()
            else:
                e = sess.query(
                    self.__class__.id).filter(
                    and_(
                        g.path.ilike(
                            "%{}%".format(p)))).scalar()
            sess.close()
            if not obj:
                e = e is not None
        else:
            log.w("Could not query for gallery existence because no path was set.")
        return e

page_profiles = profile_association("page")


@generic_repr
class Page(TaggableMixin, ProfileMixin, Base):
    __tablename__ = 'page'
    number = Column(Integer, nullable=False, default=0)
    name = Column(String, nullable=False, default='')
    path = Column(String, nullable=False, default='')
    hash_id = Column(Integer, ForeignKey('hash.id'))
    gallery_id = Column(Integer, ForeignKey('gallery.id'), nullable=False)

    hash = relationship("Hash", cascade="save-update, merge, refresh-expire")
    gallery = relationship("Gallery", back_populates="pages")
    profiles = relationship(
        "Profile",
        secondary=page_profiles,
        lazy='joined',
        cascade="all")

    @property
    def file_type(self):
        ext = ''
        if self.path:
            _, ext = os.path.splitext(self.path)
        return ext.lower()

    @validates("path")
    def _add_name(self, key, value):
        if value.endswith(constants.supported_images):
            _, self.name = os.path.split(value)

    def exists(self, obj=False):
        """Checks if page exist by path
        Params:
            obj -- queries for the full object and returns it
        """
        e = self
        if not constants.db_session:
            return e
        sess = constants.db_session()
        p = self.__class__
        if self.path:
            sess = constants.db_session()
            e = sess.query(
                p.id).filter(
                and_(
                    p.path.ilike(
                        "%{}%".format(
                            self.path)))).scalar()
            sess.close()
            if not obj:
                e = e is not None
        else:
            log.w("Could not query for page existence because no path was set.")
        return e


@generic_repr
class Title(Base):
    __tablename__ = 'title'
    name = Column(String, nullable=False, default="")  # OBS: not unique
    language_id = Column(Integer, ForeignKey('language.id'))
    gallery_id = Column(Integer, ForeignKey('gallery.id'), nullable=False)

    language = relationship(
        "Language",
        cascade="save-update, merge, refresh-expire")
    gallery = relationship("Gallery", back_populates="titles")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)


@generic_repr
class GalleryUrl(Base):
    __tablename__ = 'gallery_url'
    url = Column(String, nullable=False, default='')
    gallery_id = Column(Integer, ForeignKey('gallery.id'))

    gallery = relationship("Gallery", back_populates="urls")


# Note: necessary to put in function because there is no Session object yet
def initEvents(sess):
    "Initializes events"

    @event.listens_for(sess, 'before_commit')
    def delete_artist_orphans(session):
        session.query(Artist).filter(
            ~Artist.galleries.any()).delete(
            synchronize_session=False)

    @event.listens_for(sess, 'before_commit')
    def delete_tag_orphans(session):
        session.query(Tag).filter(
            ~Tag.namespaces.any()).delete(
            synchronize_session=False)

    @event.listens_for(sess, 'before_commit')
    def delete_namespace_orphans(session):
        session.query(Namespace).filter(
            ~Namespace.tags.any()).delete(
            synchronize_session=False)

    @event.listens_for(sess, 'before_commit')
    def delete_namespacetags_orphans(session):
        tagids = [r.id for r in session.query(Tag.id).all()]
        if tagids:
            try:
                session.query(
                    NamespaceTags.id).filter(
                    ~NamespaceTags.tag_id.in_(tagids)).delete(
                    synchronize_session=False)
            except exc.OperationalError:
                for id, tagid in session.query(
                        NamespaceTags.id, NamespaceTags.tag_id).all():
                    if tagid not in tagids:
                        session.delete(NamespaceTags.id == id)
        else:
            session.query(NamespaceTags).delete(synchronize_session=False)

    @event.listens_for(sess, 'before_commit')
    def delete_circle_orphans(session):
        session.query(Circle).filter(
            ~Circle.galleries.any()).delete(
            synchronize_session=False)

    @event.listens_for(sess, 'before_commit')
    def delete_grouping_orphans(session):
        session.query(Grouping).filter(
            ~Grouping.galleries.any()).delete(
            synchronize_session=False)


@compiles(RegexMatchExpression, 'sqlite')
def sqlite_regex_match(element, compiler, **kw):
    """Compile the SQL expression representing a regular expression match
    for the SQLite engine.
    """
    # determine the name of a custom SQLite function to use for the operator
    operator = element.operator.opstring
    try:
        func_name, _ = SQLITE_REGEX_FUNCTIONS[operator]
    except (KeyError, ValueError) as e:
        would_be_sql_string = ' '.join((compiler.process(element.left),
                                        operator,
                                        compiler.process(element.right)))
        raise exc.StatementError(
            "unknown regular expression match operator: %s" %
            operator, would_be_sql_string, None, e)

    # compile the expression as an invocation of the custom function
    regex_func = getattr(func, func_name)
    regex_func_call = regex_func(element.left, element.right)
    return compiler.process(regex_func_call)


@event.listens_for(Engine, 'connect')
def sqlite_engine_connect(dbapi_connection, connection_record):
    """Listener for the event of establishing connection to a SQLite database.
    Creates the functions handling regular expression operators
    within SQLite engine, pointing them to their Python implementations above.
    """

    for name, function in SQLITE_REGEX_FUNCTIONS.values():
        dbapi_connection.create_function(name, 2, function)


def init_defaults(sess):
    "Initializes default items"

    # init default user
    duser = sess.query(User).filter(
        User.role == User.Role.default).one_or_none()
    if not duser:
        duser = User(name="default", role=User.Role.default)
        sess.add(duser)
        sess.commit()
    constants.default_user = duser


def create_user(role, name=None, password=None):
    """
    Create user
    role:
        - default -- create a default user with no password if it doesnt exist
        - admin -- create admin user
        - user -- create regular user
    """
    assert isinstance(role, User.Role)

    s = constants.db_session()

    if role == User.Role.default:
        if not s.query(User).filter(User.name == 'default').one_or_none():
            s.add(User(name='default', role=User.Role.default))
            s.commit()

    elif role == User.Role.user:
        pass
    elif role == User.Role.admin:
        s.add(User(name=name,
                   password=PasswordHash.new(password, 15),
                   role=User.Role.admin))
        s.commit()


def check_db_version(sess):
    """Checks if DB version is allowed.
    Raises db exception if not"""
    try:
        life = sess.query(Life).one_or_none()
    except exc.NoSuchTableError:
        raise exceptions.DatabaseInitError(
            "Invalid database. NoSuchTableError.")
    if life:
        if life.version not in constants.version_db:
            msg = 'Local database version: {}\nSupported database versions:{}'.format(
                life.version, constants.version_db)
            log.c("Incompatible database version")
            log.d(msg)
            raise exceptions.DatabaseVersionError(msg)
    else:
        life = Life()
        sess.add(life)
        life.version = constants.version_db[0]
        life.times_opened = 0
        sess.commit()
        log.i("Succesfully initiated database")
        log.i("DB Version: {}".format(life.version))

    life.times_opened += 1
    sess.add(Event(life, Event.Action.start))
    init_defaults(sess)
    sess.commit()
    return True


def init(**kwargs):
    db_path = constants.db_path_dev if constants.dev else constants.db_path
    Session = scoped_session(sessionmaker())
    constants.db_session = Session
    initEvents(constants.db_session)
    engine = create_engine(os.path.join("sqlite:///", db_path))
    Base.metadata.create_all(engine)
    Session.configure(bind=engine)

    return check_db_version(Session())


def add_bulk(session, objects, amount=100):
    """
    Add objects in a bulk of x amount
    """
    left = objects[:amount]
    while left:
        session.add_all(left)
        session.commit()
        left = objects[:amount]


def table_attribs(model, id=False):
    """Returns a dict of table column names and their SQLAlchemy value objects
    Params:
        id -- retrieve id columns instead of the sqlalchemy object (to avoid a db query etc.)
    """
    assert isinstance(model, Base)
    d = {}

    obj = inspect(model)
    if isinstance(obj, state.InstanceState):
        attr = list(obj.attrs)

        exclude = [y.key for y in attr if y.key.endswith('_id')]
        if id:
            exclude = [x[:-3] for x in exclude]  # -3 for '_id'

        for x in attr:
            if x.key not in exclude:
                d[x.key] = x.value

    else:
        for name in model.__dict__:
            value = model.__dict__[name]
            if isinstance(value, attributes.InstrumentedAttribute):
                d[name] = value
    return d


def is_instanced(obj):
    "Check if db object is an instanced object"
    if isinstance(obj, Base):
        return isinstance(inspect(obj), state.InstanceState)
    return False


def is_list(obj):
    "Check if db object is a db list"
    return isinstance(obj, collections.InstrumentedList)


def is_query(obj):
    "Check if db object is a dynamic query object (issued by lazy='dynamic')"
    return isinstance(obj, dynamic.AppenderQuery)


def related_classes(obj):
    "Returns a list of mapper classes that has a relationship with given mapper class"
    assert issubclass(obj, Base)

    return [x.mapper.class_ for x in inspect(obj).relationships]


def model_name(model):
    "Return name of model"
    assert issubclass(model, Base)
    return model.__name__
