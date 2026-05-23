from sqlalchemy import Column, Integer, String, Text, Boolean, ForeignKey, Table
from sqlalchemy.orm import relationship
from app.database import Base

# Association tables for many-to-many relationships
book_authors = Table(
    'book_authors',
    Base.metadata,
    Column('book_id', Integer, ForeignKey('book.id')),
    Column('person_id', Integer, ForeignKey('person.id'))
)

book_editors = Table(
    'book_editors',
    Base.metadata,
    Column('book_id', Integer, ForeignKey('book.id')),
    Column('person_id', Integer, ForeignKey('person.id'))
)

book_translators = Table(
    'book_translators',
    Base.metadata,
    Column('book_id', Integer, ForeignKey('book.id')),
    Column('person_id', Integer, ForeignKey('person.id'))
)

book_bookshelves = Table(
    'book_bookshelves',
    Base.metadata,
    Column('book_id', Integer, ForeignKey('book.id')),
    Column('bookshelf_id', Integer, ForeignKey('bookshelf.id'))
)

book_languages = Table(
    'book_languages',
    Base.metadata,
    Column('book_id', Integer, ForeignKey('book.id')),
    Column('language_id', Integer, ForeignKey('language.id'))
)

book_subjects = Table(
    'book_subjects',
    Base.metadata,
    Column('book_id', Integer, ForeignKey('book.id')),
    Column('subject_id', Integer, ForeignKey('subject.id'))
)


class Book(Base):
    __tablename__ = "book"
    
    id = Column(Integer, primary_key=True, index=True)
    gutenberg_id = Column(Integer, unique=True, index=True)
    title = Column(String(1024), nullable=True)
    copyright = Column(Boolean, nullable=True)
    download_count = Column(Integer, nullable=True)
    media_type = Column(String(16))
    
    # Relationships
    authors = relationship(
        "Person",
        secondary=book_authors,
        back_populates="books"
    )
    editors = relationship(
        "Person",
        secondary=book_editors,
        back_populates="books_edited"
    )
    translators = relationship(
        "Person",
        secondary=book_translators,
        back_populates="books_translated"
    )
    bookshelves = relationship(
        "Bookshelf",
        secondary=book_bookshelves,
        back_populates="books"
    )
    languages = relationship(
        "Language",
        secondary=book_languages,
        back_populates="books"
    )
    subjects = relationship(
        "Subject",
        secondary=book_subjects,
        back_populates="books"
    )
    formats = relationship("Format", back_populates="book", cascade="all, delete-orphan")
    summaries = relationship("Summary", back_populates="book", cascade="all, delete-orphan")


class Person(Base):
    __tablename__ = "person"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), index=True)
    birth_year = Column(Integer, nullable=True)
    death_year = Column(Integer, nullable=True)
    
    # Relationships
    books = relationship(
        "Book",
        secondary=book_authors,
        back_populates="authors"
    )
    books_edited = relationship(
        "Book",
        secondary=book_editors,
        back_populates="editors"
    )
    books_translated = relationship(
        "Book",
        secondary=book_translators,
        back_populates="translators"
    )


class Bookshelf(Base):
    __tablename__ = "bookshelf"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), unique=True, index=True)
    
    # Relationships
    books = relationship(
        "Book",
        secondary=book_bookshelves,
        back_populates="bookshelves"
    )


class Language(Base):
    __tablename__ = "language"
    
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(4), unique=True, index=True)
    
    # Relationships
    books = relationship(
        "Book",
        secondary=book_languages,
        back_populates="languages"
    )


class Subject(Base):
    __tablename__ = "subject"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), index=True)
    
    # Relationships
    books = relationship(
        "Book",
        secondary=book_subjects,
        back_populates="subjects"
    )


class Format(Base):
    __tablename__ = "format"
    
    id = Column(Integer, primary_key=True, index=True)
    book_id = Column(Integer, ForeignKey('book.id'))
    mime_type = Column(String(32))
    url = Column(String(256))
    
    # Relationships
    book = relationship("Book", back_populates="formats")


class Summary(Base):
    __tablename__ = "summary"
    
    id = Column(Integer, primary_key=True, index=True)
    book_id = Column(Integer, ForeignKey('book.id'))
    text = Column(Text)
    
    # Relationships
    book = relationship("Book", back_populates="summaries")
