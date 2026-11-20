from __future__ import annotations

import os
from datetime import date, datetime

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String,
                        Text, create_engine)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR}/sleep.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Night(Base):
    __tablename__ = "nights"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    night_date: Mapped[date] = mapped_column(Date, index=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    start: Mapped[datetime | None] = mapped_column(DateTime)
    end: Mapped[datetime | None] = mapped_column(DateTime)
    engine: Mapped[str] = mapped_column(String(32), default="heuristic")
    has_resmed: Mapped[bool] = mapped_column(Boolean, default=False)
    has_o2ring: Mapped[bool] = mapped_column(Boolean, default=False)
    cpap_offset_s: Mapped[float] = mapped_column(Float, default=0.0)
    age: Mapped[int | None] = mapped_column(Integer)
    gender: Mapped[str | None] = mapped_column(String(8))
    notes: Mapped[str | None] = mapped_column(Text)
    files: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    oximetry: Mapped[dict | None] = mapped_column(JSON)

    epochs: Mapped[list["Epoch"]] = relationship(
        back_populates="night", cascade="all, delete-orphan", order_by="Epoch.idx")
    events: Mapped[list["Event"]] = relationship(
        back_populates="night", cascade="all, delete-orphan", order_by="Event.t")


class Epoch(Base):
    __tablename__ = "epochs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    night_id: Mapped[int] = mapped_column(ForeignKey("nights.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer)
    t: Mapped[datetime] = mapped_column(DateTime)
    stage: Mapped[str] = mapped_column(String(8))
    resp_rate: Mapped[float | None] = mapped_column(Float)
    breath_cv: Mapped[float | None] = mapped_column(Float)
    amp_cv: Mapped[float | None] = mapped_column(Float)
    flow_present: Mapped[float | None] = mapped_column(Float)
    resp_events: Mapped[float | None] = mapped_column(Float)
    hr: Mapped[float | None] = mapped_column(Float)
    hr_std: Mapped[float | None] = mapped_column(Float)
    spo2: Mapped[float | None] = mapped_column(Float)
    spo2_min: Mapped[float | None] = mapped_column(Float)
    motion: Mapped[float | None] = mapped_column(Float)
    wake_score: Mapped[float | None] = mapped_column(Float)
    rem_score: Mapped[float | None] = mapped_column(Float)
    night: Mapped[Night] = relationship(back_populates="epochs")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    night_id: Mapped[int] = mapped_column(ForeignKey("nights.id", ondelete="CASCADE"), index=True)
    t: Mapped[datetime] = mapped_column(DateTime)
    duration_s: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16))
    night: Mapped[Night] = relationship(back_populates="events")


EPOCH_FIELDS = ["resp_rate", "breath_cv", "amp_cv", "flow_present", "resp_events", "hr",
                "hr_std", "spo2", "spo2_min", "motion", "wake_score", "rem_score"]


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    Base.metadata.create_all(engine)
