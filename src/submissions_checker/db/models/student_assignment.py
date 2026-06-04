"""Student assignment — abstract enrollment of a student in an assignment, holds final grade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from submissions_checker.db.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from submissions_checker.db.models.student import Student
    from submissions_checker.db.models.subjects_assignment import SubjectsAssignment
    from submissions_checker.db.models.submission import Submission


class StudentAssignment(Base, TimestampMixin):
    __tablename__ = "students_assignments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    student_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("students.id", ondelete="RESTRICT"), nullable=False
    )
    subjects_assignment_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects_assignments.id", ondelete="RESTRICT"), nullable=False
    )
    grade: Mapped[int | None] = mapped_column(Integer, nullable=True)
    variant: Mapped[str | None] = mapped_column(String(50), nullable=True)

    student: Mapped[Student] = relationship("Student", back_populates="students_assignments")
    subjects_assignment: Mapped[SubjectsAssignment] = relationship(
        "SubjectsAssignment", back_populates="students_assignments"
    )
    submissions: Mapped[list[Submission]] = relationship(
        "Submission", back_populates="students_assignment"
    )

    __table_args__ = (
        UniqueConstraint("student_id", "subjects_assignment_id", name="uq_students_assignments"),
        Index("ix_students_assignments_student_id", "student_id"),
        Index("ix_students_assignments_subjects_assignment_id", "subjects_assignment_id"),
    )
