from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Class(Base):
    __tablename__ = "classes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    class_code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    class_name: Mapped[str] = mapped_column(String(200), nullable=False)
    teacher_name: Mapped[str] = mapped_column(String(200), nullable=False)
    required_grade: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("required_grade >= 0 AND required_grade <= 12", name="required_grade_range"),
    )


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Preference(Base):
    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    recorded_by: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    student_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    student_grade: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    good_class_id: Mapped[int] = mapped_column(ForeignKey("classes.id", ondelete="RESTRICT"), nullable=False, index=True)
    bad_class_id: Mapped[int] = mapped_column(ForeignKey("classes.id", ondelete="RESTRICT"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    __table_args__ = (
        CheckConstraint("student_grade >= 9 AND student_grade <= 12", name="student_grade_range"),
        CheckConstraint("good_class_id <> bad_class_id", name="different_class_pair"),
    )
