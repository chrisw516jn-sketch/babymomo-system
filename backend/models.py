from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, Index
from sqlalchemy.sql import func
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(120), unique=True, index=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    display_name = Column(String(100), nullable=False)
    role = Column(String(30), default="staff")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class Measurement(Base):
    __tablename__ = "measurements"

    id = Column(Integer, primary_key=True, index=True)
    id_card = Column(String(20), index=True, nullable=False)
    user_name = Column(String(50), index=True, nullable=False)
    gender = Column(String(5), nullable=False)
    age = Column(Integer)
    height = Column(Float)
    weight = Column(Float)
    bmi = Column(Float)
    body_fat = Column(Float)
    smi = Column(Float)
    systolic = Column(Integer)
    diastolic = Column(Integer)
    pulse = Column(Integer)
    grip_strength = Column(Float)
    chair_stand_time = Column(Float)
    walking_time = Column(Float)
    sarcopenia_stage = Column(String(30), index=True)
    abnormal_count = Column(Integer, default=0)
    status = Column(Text)
    measure_date = Column(String(10), index=True)
    measure_time = Column(String(19), index=True)
    source = Column(String(30), default="manual")
    created_by = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_idcard_measuretime", "id_card", "measure_time", unique=True),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    operator = Column(String(50))
    action = Column(String(100))
    details = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)
    measurement_id = Column(Integer, index=True, nullable=True)
    id_card = Column(String(20), index=True, nullable=False)
    user_name = Column(String(50), nullable=False)
    severity = Column(String(20), default="warning")
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    sarcopenia_stage = Column(String(30), nullable=True)
    abnormal_count = Column(Integer, default=0)
    target_roles = Column(String(100), default="nurse,caregiver")
    is_read = Column(Boolean, default=False)
    read_by = Column(String(50), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    is_handled = Column(Boolean, default=False)
    handled_by = Column(String(50), nullable=True)
    handled_at = Column(DateTime(timezone=True), nullable=True)
    handle_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CareNote(Base):
    """關懷追蹤紀錄"""
    __tablename__ = "care_notes"

    id = Column(Integer, primary_key=True, index=True)
    id_card = Column(String(20), index=True, nullable=False)
    user_name = Column(String(50), nullable=True)
    alert_id = Column(Integer, index=True, nullable=True)
    note_type = Column(String(30), default="followup")  # followup / phone / referral / retest
    content = Column(Text, nullable=False)
    next_follow_date = Column(String(10), nullable=True)
    created_by = Column(String(50))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SystemConfig(Base):
    """系統設定（異常門檻等）"""
    __tablename__ = "system_config"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(50), unique=True, index=True, nullable=False)
    value = Column(Text, nullable=False)
    updated_by = Column(String(50), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Feedback(Base):
    """意見／錯誤回饋"""
    __tablename__ = "feedbacks"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(30), default="suggestion", index=True)  # suggestion / bug / other
    title = Column(String(200), nullable=True)
    content = Column(Text, nullable=False)
    contact = Column(String(120), nullable=True)
    page_url = Column(String(300), nullable=True)
    created_by = Column(String(50), nullable=True)
    created_by_name = Column(String(100), nullable=True)
    line_sent = Column(Boolean, default=False)
    line_result = Column(Text, nullable=True)
    is_handled = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class CaseEquipmentPlan(Base):
    """個案客製化運動輔具建議（每人一組，非群體共用）"""
    __tablename__ = "case_equipment_plans"

    id = Column(Integer, primary_key=True, index=True)
    id_card = Column(String(20), unique=True, index=True, nullable=False)
    user_name = Column(String(50), nullable=True)
    # JSON list: [{name, why, how, caution}, ...]
    items_json = Column(Text, nullable=False, default="[]")
    note = Column(Text, nullable=True)
    updated_by = Column(String(50), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SystemIssue(Base):
    """系統自動健檢發現的問題（可自動排除或待管理員處理）"""
    __tablename__ = "system_issues"

    id = Column(Integer, primary_key=True, index=True)
    issue_type = Column(String(50), index=True, nullable=False)
    severity = Column(String(20), default="warning")
    title = Column(String(200), nullable=False)
    detail = Column(Text, nullable=True)
    related_id = Column(String(50), nullable=True)
    fingerprint = Column(String(120), index=True, nullable=True)
    auto_fixed = Column(Boolean, default=False)
    status = Column(String(20), default="open", index=True)  # open / fixed / ignored
    resolved_by = Column(String(50), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolve_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ExerciseLog(Base):
    """運動輔具／有氧／握力訓練紀錄（與體適能量測分開）"""
    __tablename__ = "exercise_logs"

    id = Column(Integer, primary_key=True, index=True)
    id_card = Column(String(20), index=True, nullable=False)
    user_name = Column(String(50), index=True, nullable=True)
    gender = Column(String(5), nullable=True)
    exercise_type = Column(String(40), index=True, nullable=False)  # ResistanceExercise / AerobicExercise / GripTraining
    machine_code = Column(String(20), nullable=True)
    machine_name = Column(String(80), nullable=True)
    duration_sec = Column(Float, nullable=True)
    reps = Column(Float, nullable=True)
    calories = Column(Float, nullable=True)
    distance_km = Column(Float, nullable=True)
    achievement_rate = Column(Float, nullable=True)
    extra = Column(Text, nullable=True)  # JSON 或摘要
    summary = Column(String(500), nullable=True)
    measure_date = Column(String(10), index=True)
    measure_time = Column(String(19), index=True)
    machine_number = Column(String(50), nullable=True)
    source = Column(String(30), default="netown")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
