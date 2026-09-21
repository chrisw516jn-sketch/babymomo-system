from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query, status, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, desc
from typing import List, Optional
from datetime import timedelta, datetime, timezone
import io
import csv
import os
import json
from openpyxl import load_workbook

# 自動載入 backend/.env（LINE Token 等），之後不用每次手動設環境變數
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except Exception:
    pass

from database import engine, get_db, Base
from models import User, Measurement, AuditLog, Alert, CareNote, SystemConfig, Feedback
from schemas import (
    UserCreate, UserUpdate, UserOut, Token, MeasurementCreate, MeasurementOut,
    StatsOut, ImportResult, CareNoteCreate, CareNoteOut, ThresholdsOut,
    FeedbackCreate, FeedbackOut,
)
from auth import (
    get_password_hash, verify_password, create_access_token,
    get_current_user, require_roles, get_user_by_username, ACCESS_TOKEN_EXPIRE_MINUTES,
)
from utils import (
    calc_bmi, judge_sarcopenia, normalize_measure_time, format_alert_message,
    bp_status, get_exercise_advice, suggested_retest_date, get_intervention_plan,
    get_default_equipment_catalog,
)
from line_notify import send_line_text, line_configured
from standards import compare_to_standards, walking_threshold

app = FastAPI(
    title="寶貝機 長者體適能與肌少衰弱檢測分析系統 API",
    description="真實可用的後端 API，支援 CSV/Excel 匯入、多人登入、RESTful 串接",
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_THRESHOLDS = {
    "grip_male": 28.0,
    "grip_female": 18.0,
    "smi_male": 7.0,
    "smi_female": 5.7,
    "chair_stand": 12.0,
    "walking_time": 20.0,
    "systolic_high": 140,
    "diastolic_high": 90,
}


def get_thresholds(db: Session) -> dict:
    row = db.query(SystemConfig).filter(SystemConfig.key == "thresholds").first()
    if not row:
        return dict(DEFAULT_THRESHOLDS)
    try:
        data = json.loads(row.value)
        out = dict(DEFAULT_THRESHOLDS)
        out.update(data)
        return out
    except Exception:
        return dict(DEFAULT_THRESHOLDS)


def get_equipment_catalog(db: Session) -> list:
    """讀取管理員可編輯的運動輔具建議目錄。"""
    row = db.query(SystemConfig).filter(SystemConfig.key == "equipment_catalog").first()
    if not row:
        return get_default_equipment_catalog()
    try:
        data = json.loads(row.value)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return get_default_equipment_catalog()


def _alert_item_dict(db: Session, a: Alert) -> dict:
    vitals = _vitals_for_alert(db, a)
    plan = get_intervention_plan(
        stage=a.sarcopenia_stage or vitals.get("sarcopenia_stage"),
        grip=vitals.get("grip_strength"),
        chair=vitals.get("chair_stand_time"),
        walk=vitals.get("walking_time"),
        smi=vitals.get("smi"),
        gender=vitals.get("gender"),
        bp_status_text=vitals.get("bp_status"),
        equipment_catalog=get_equipment_catalog(db),
    )
    return {
        "id": a.id,
        "measurement_id": a.measurement_id,
        "id_card": a.id_card,
        "user_name": a.user_name,
        "severity": a.severity,
        "title": a.title,
        "message": a.message,
        "sarcopenia_stage": a.sarcopenia_stage,
        "abnormal_count": a.abnormal_count,
        "is_read": a.is_read,
        "read_by": a.read_by,
        "is_handled": a.is_handled,
        "handled_by": a.handled_by,
        "handle_note": a.handle_note,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "vitals": vitals,
        "intervention_plan": plan,
    }


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        keep = {"bonnie", "chrisavicii", "littlethanks", "Netown", "netown", "nurse1", "care1"}
        for old in db.query(User).all():
            if old.username not in keep:
                db.delete(old)
        db.commit()

        defaults = [
            ("bonnie", "Aa960723", "Bonnie (系統管理員)", "superadmin", "Bonnie960723@gmail.com"),
            ("chrisavicii", "Aa0965652118", "Chris (超級管理員)", "superadmin", "chrisw516jn@gmail.com"),
            ("littlethanks", "Aa0610", "館管理員", "admin", None),
            ("Netown", "Aa0225991228", "Netown (管理員)", "admin", None),
            ("nurse1", "Nurse1234", "護理師小美", "nurse", None),
            ("care1", "Care1234", "照顧服務員小華", "caregiver", None),
        ]
        for username, pwd, display, role, email in defaults:
            existing = get_user_by_username(db, username)
            if existing:
                existing.hashed_password = get_password_hash(pwd)
                existing.display_name = display
                existing.role = role
                existing.email = email
                existing.is_active = True
            else:
                db.add(User(
                    username=username,
                    hashed_password=get_password_hash(pwd),
                    display_name=display,
                    role=role,
                    email=email,
                    is_active=True,
                ))
        db.commit()

        if not db.query(SystemConfig).filter(SystemConfig.key == "thresholds").first():
            db.add(SystemConfig(key="thresholds", value=json.dumps(DEFAULT_THRESHOLDS), updated_by="system"))
            db.commit()
    finally:
        db.close()


def _norm_id(v) -> str:
    return str(v or "").strip().upper()


def _fp_key(id_card, measure_time, grip, chair, walk, smi, systolic=None):
    mt = str(measure_time or "")[:16]

    def r(x, n=1):
        if x is None or x == "":
            return None
        try:
            return round(float(x), n)
        except Exception:
            return None

    return (
        _norm_id(id_card),
        mt,
        r(grip),
        r(chair, 2),
        r(walk, 2),
        r(smi, 1),
        r(systolic, 0),
    )


def purge_duplicate_measurements(db: Session) -> tuple:
    rows = db.query(Measurement).order_by(Measurement.id.asc()).all()
    seen_time = {}
    seen_fp = {}
    to_delete = []
    for rec in rows:
        tkey = (_norm_id(rec.id_card), rec.measure_time or "")
        fkey = _fp_key(
            rec.id_card, rec.measure_time, rec.grip_strength,
            rec.chair_stand_time, rec.walking_time, rec.smi, rec.systolic,
        )
        dup = False
        if tkey in seen_time:
            dup = True
        else:
            seen_time[tkey] = rec.id
        if fkey in seen_fp:
            dup = True
        else:
            seen_fp[fkey] = rec.id
        if dup:
            to_delete.append(rec)
    names = []
    for rec in to_delete:
        names.append(f"{rec.user_name}/{rec.id_card} @ {rec.measure_time}")
        db.delete(rec)
    if to_delete:
        db.commit()
    return len(to_delete), names[:40]


def add_audit(db: Session, operator: str, action: str, details: str = ""):
    db.add(AuditLog(operator=operator, action=action, details=details))
    db.commit()


def create_abnormal_alert(db: Session, rec: Measurement):
    if not rec:
        return None
    stage = rec.sarcopenia_stage or "正常"
    abn = rec.abnormal_count or 0
    if abn <= 0 and stage == "正常":
        return None

    if stage == "嚴重肌少症" or abn >= 3:
        severity = "critical"
        title = f"【緊急】{rec.user_name} 身體數據多重異常"
        advice = "建議：盡快關懷，必要時轉介醫療或安排兩週內複測。"
    elif stage in ("肌少症", "肌少症前期") or abn >= 1:
        severity = "warning"
        title = f"【注意】{rec.user_name} 檢測異常需關懷"
        advice = "建議：電話或現場關心，並記錄關懷追蹤；可安排 2～4 週複測。"
    else:
        severity = "info"
        title = f"【提醒】{rec.user_name} 指標需追蹤"
        advice = "建議：持續觀察，下次檢測時比對趨勢。"

    message = format_alert_message(rec) + "\n" + advice

    alert = Alert(
        measurement_id=rec.id,
        id_card=rec.id_card,
        user_name=rec.user_name,
        severity=severity,
        title=title,
        message=message,
        sarcopenia_stage=stage,
        abnormal_count=abn,
        target_roles="nurse,caregiver,superadmin,admin",
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    try:
        send_line_text(message)
    except Exception:
        pass
    return alert


# ========== Auth ==========
@app.post("/api/auth/register", response_model=UserOut, tags=["Auth"])
def register(payload: UserCreate, db: Session = Depends(get_db)):
    raise HTTPException(403, "公開註冊已關閉，請使用管理員帳號登入")


@app.post("/api/auth/login", response_model=Token, tags=["Auth"])
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    username = (form_data.username or "").strip()
    password = (form_data.password or "").strip()
    user = get_user_by_username(db, username)
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "帳號或密碼錯誤")
    if not user.is_active:
        raise HTTPException(403, "帳號已停用")
    token = create_access_token(
        data={"sub": user.username},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    add_audit(db, user.username, "login", "登入成功")
    return Token(access_token=token, user=UserOut.model_validate(user))


@app.get("/api/auth/me", response_model=UserOut, tags=["Auth"])
def me(current_user: User = Depends(get_current_user)):
    return current_user


# ========== Users 帳號管理 ==========
@app.get("/api/users", tags=["Users"])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    users = db.query(User).order_by(User.id.asc()).all()
    return [UserOut.model_validate(u) for u in users]


@app.post("/api/users", response_model=UserOut, tags=["Users"])
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    if get_user_by_username(db, payload.username):
        raise HTTPException(400, "帳號已存在")
    allowed_roles = {"superadmin", "admin", "nurse", "caregiver", "staff", "viewer"}
    if payload.role not in allowed_roles:
        raise HTTPException(400, f"角色必須是：{', '.join(sorted(allowed_roles))}")
    if current_user.role != "superadmin" and payload.role in ("superadmin",):
        raise HTTPException(403, "只有超級管理員可建立超級管理員")
    user = User(
        username=payload.username.strip(),
        hashed_password=get_password_hash(payload.password),
        display_name=payload.display_name,
        email=payload.email,
        role=payload.role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    add_audit(db, current_user.username, "create_user", f"{user.username} role={user.role}")
    return user


@app.patch("/api/users/{user_id}", response_model=UserOut, tags=["Users"])
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    user = db.query(User).get(user_id)
    if not user:
        raise HTTPException(404, "找不到帳號")
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.email is not None:
        user.email = payload.email
    if payload.role is not None:
        if current_user.role != "superadmin" and payload.role == "superadmin":
            raise HTTPException(403, "只有超級管理員可指定超級管理員")
        user.role = payload.role
    if payload.is_active is not None:
        if user.username == current_user.username and not payload.is_active:
            raise HTTPException(400, "不能停用自己的帳號")
        user.is_active = payload.is_active
    if payload.password:
        user.hashed_password = get_password_hash(payload.password)
    db.commit()
    db.refresh(user)
    add_audit(db, current_user.username, "update_user", f"{user.username}")
    return user


# ========== Measurements ==========
@app.post("/api/measurements", response_model=MeasurementOut, tags=["Measurements"])
def create_measurement(
    payload: MeasurementCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    measure_date, measure_time = normalize_measure_time(payload.measure_time)
    exists = db.query(Measurement).filter(
        Measurement.id_card == payload.id_card,
        Measurement.measure_time == measure_time,
    ).first()
    if exists:
        raise HTTPException(409, "同一身分證於同一時間點已有紀錄，已略過")

    bmi = calc_bmi(payload.height, payload.weight) or payload.bmi
    stage, abn_count, status_text = judge_sarcopenia(
        payload.gender, payload.grip_strength, payload.chair_stand_time,
        payload.walking_time, payload.smi, age=payload.age
    )
    th = get_thresholds(db)
    if payload.systolic and payload.systolic >= int(th.get("systolic_high", 140)):
        if status_text == "各項指標正常":
            status_text = f"血壓偏高 ({payload.systolic}/{payload.diastolic or '?'})"
        else:
            status_text += f" / 血壓偏高 ({payload.systolic}/{payload.diastolic or '?'})"
        abn_count += 1

    rec = Measurement(
        id_card=payload.id_card,
        user_name=payload.user_name,
        gender=payload.gender,
        age=payload.age,
        height=payload.height,
        weight=payload.weight,
        bmi=bmi,
        body_fat=payload.body_fat,
        smi=payload.smi,
        systolic=payload.systolic,
        diastolic=payload.diastolic,
        pulse=payload.pulse,
        grip_strength=payload.grip_strength,
        chair_stand_time=payload.chair_stand_time,
        walking_time=payload.walking_time,
        sarcopenia_stage=stage,
        abnormal_count=abn_count,
        status=status_text,
        measure_date=measure_date,
        measure_time=measure_time,
        source="api",
        created_by=current_user.username,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    create_abnormal_alert(db, rec)
    add_audit(db, current_user.username, "create_measurement", f"{payload.id_card} {measure_time}")
    return rec


def _same_day_latest_ids(db: Session) -> set:
    """同一身分證同一天只保留時間最新的一筆。"""
    rows = db.query(
        Measurement.id, Measurement.id_card, Measurement.measure_date, Measurement.measure_time
    ).all()
    latest = {}
    for rid, card, day, mt in rows:
        key = (str(card or "").strip().upper(), str(day or ""))
        cur = str(mt or "")
        if key not in latest or cur > str(latest[key][1]):
            latest[key] = (rid, cur)
    return {v[0] for v in latest.values()}


@app.get("/api/measurements", tags=["Measurements"])
def list_measurements(
    q: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    stage: Optional[str] = None,
    gender: Optional[str] = None,
    abnormal_only: bool = False,
    include_duplicates: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Measurement)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Measurement.id_card.ilike(like),
            Measurement.user_name.ilike(like),
            Measurement.sarcopenia_stage.ilike(like),
        ))
    if start_date:
        query = query.filter(Measurement.measure_date >= start_date)
    if end_date:
        query = query.filter(Measurement.measure_date <= end_date)
    if stage:
        query = query.filter(Measurement.sarcopenia_stage == stage)
    if gender:
        g = "M" if gender.upper() in ("M", "男") else "F"
        query = query.filter(Measurement.gender == g)
    if abnormal_only:
        query = query.filter(or_(Measurement.abnormal_count > 0, Measurement.sarcopenia_stage != "正常"))
    if not include_duplicates:
        keep = _same_day_latest_ids(db)
        if keep:
            query = query.filter(Measurement.id.in_(keep))

    total = query.count()
    items = (
        query.order_by(Measurement.measure_time.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [MeasurementOut.model_validate(i) for i in items],
    }


@app.get("/api/duplicates", tags=["Measurements"])
def list_duplicates(
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """同一天被較新資料取代的舊筆，可在重複專區查詢。"""
    keep = _same_day_latest_ids(db)
    query = db.query(Measurement)
    if keep:
        query = query.filter(~Measurement.id.in_(keep))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Measurement.id_card.ilike(like),
            Measurement.user_name.ilike(like),
        ))
    total = query.count()
    items = (
        query.order_by(Measurement.measure_time.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [MeasurementOut.model_validate(i) for i in items],
    }


@app.get("/api/measurements/{record_id}", response_model=MeasurementOut, tags=["Measurements"])
def get_measurement(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rec = db.query(Measurement).get(record_id)
    if not rec:
        raise HTTPException(404, "找不到紀錄")
    return rec


@app.delete("/api/measurements/{record_id}", tags=["Measurements"])
def delete_measurement(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin", "company_admin")),
):
    rec = db.query(Measurement).get(record_id)
    if not rec:
        raise HTTPException(404, "找不到紀錄")
    db.delete(rec)
    db.commit()
    add_audit(db, current_user.username, "delete_measurement", f"id={record_id}")
    return {"ok": True}


# ========== Import ==========
@app.post("/api/import", response_model=ImportResult, tags=["Import"])
async def import_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    filename = (file.filename or "").lower()
    content = await file.read()
    rows = []
    try:
        if filename.endswith(".csv"):
            text = content.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
        elif filename.endswith((".xlsx", ".xls")):
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            headers = [str(c.value).strip() if c.value is not None else "" for c in next(ws.iter_rows(min_row=1, max_row=1))]
            for row in ws.iter_rows(min_row=2, values_only=True):
                d = {}
                for i, h in enumerate(headers):
                    if h:
                        d[h] = row[i] if i < len(row) else None
                rows.append(d)
            wb.close()
        else:
            raise HTTPException(400, "只支援 .csv / .xlsx / .xls 檔案")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"檔案解析失敗：{e}")

    if not rows:
        raise HTTPException(400, "檔案沒有資料列")

    col_map = {
        "id_card": ["id_card", "身分證", "身分證字號", "id", "idcard"],
        "user_name": ["user_name", "姓名", "name", "長者姓名"],
        "gender": ["gender", "性別", "sex"],
        "age": ["age", "年齡"],
        "height": ["height", "身高", "身高cm"],
        "weight": ["weight", "體重", "體重kg"],
        "body_fat": ["body_fat", "體脂", "體脂率", "體脂肪"],
        "smi": ["smi", "肌肉量", "骨骼肌量", "smi_kgm2"],
        "systolic": ["systolic", "收縮壓", "sbp"],
        "diastolic": ["diastolic", "舒張壓", "dbp"],
        "pulse": ["pulse", "脈搏", "心率", "hr"],
        "grip_strength": ["grip_strength", "握力", "握力kg"],
        "chair_stand_time": ["chair_stand_time", "五次坐站", "坐站", "chair"],
        "walking_time": ["walking_time", "走路時間", "步速", "walk"],
        "measure_time": ["measure_time", "檢測時間", "量測時間", "時間", "datetime", "date"],
    }
    sample_keys = list(rows[0].keys())
    lower_cols = {str(c).strip().lower(): str(c).strip() for c in sample_keys}
    resolved = {}
    for key, aliases in col_map.items():
        for a in aliases:
            if a.lower() in lower_cols:
                resolved[key] = lower_cols[a.lower()]
                break
    if "id_card" not in resolved or "user_name" not in resolved:
        raise HTTPException(400, "檔案必須至少包含「身分證」與「姓名」欄位")

    success = skipped = failed = 0
    messages = []
    seen_in_file_time = set()
    seen_in_file_fp = set()
    th = get_thresholds(db)

    for idx, row in enumerate(rows):
        try:
            def get(key, default=None):
                col = resolved.get(key)
                if not col:
                    return default
                val = row.get(col)
                if val is None or str(val).strip() == "":
                    return default
                return str(val).strip()

            id_card = get("id_card")
            user_name = get("user_name")
            if not id_card or not user_name:
                failed += 1
                messages.append(f"第 {idx+2} 列：缺少身分證或姓名")
                continue

            gender_raw = get("gender", "F")
            gender = "M" if str(gender_raw).upper() in ("M", "男", "MALE") else "F"

            def to_float(v):
                if v is None:
                    return None
                try:
                    return float(str(v).replace(",", ""))
                except Exception:
                    return None

            def to_int(v):
                f = to_float(v)
                return int(f) if f is not None else None

            height = to_float(get("height"))
            weight = to_float(get("weight"))
            grip = to_float(get("grip_strength"))
            chair = to_float(get("chair_stand_time"))
            walk = to_float(get("walking_time"))
            smi = to_float(get("smi"))
            systolic = to_int(get("systolic"))
            diastolic = to_int(get("diastolic"))
            pulse = to_int(get("pulse"))
            body_fat = to_float(get("body_fat"))
            age = to_int(get("age"))

            if grip is not None and (grip < 0 or grip > 80):
                failed += 1
                messages.append(f"第 {idx+2} 列：握力數值異常 ({grip})")
                continue
            if systolic is not None and (systolic < 50 or systolic > 250):
                failed += 1
                messages.append(f"第 {idx+2} 列：收縮壓異常 ({systolic})")
                continue

            measure_date, measure_time = normalize_measure_time(get("measure_time"))
            tkey = (_norm_id(id_card), measure_time)
            fkey = _fp_key(id_card, measure_time, grip, chair, walk, smi, systolic)

            if tkey in seen_in_file_time or fkey in seen_in_file_fp:
                skipped += 1
                messages.append(f"第 {idx+2} 列：檔案內重複，已略過（{user_name}）")
                continue
            seen_in_file_time.add(tkey)
            seen_in_file_fp.add(fkey)

            exists = db.query(Measurement).filter(
                Measurement.id_card == id_card,
                Measurement.measure_time == measure_time,
            ).first()
            if exists:
                skipped += 1
                messages.append(f"第 {idx+2} 列：與資料庫時間重複，已略過（{user_name} {measure_time}）")
                continue

            same_content = db.query(Measurement).filter(
                Measurement.id_card == id_card,
                Measurement.measure_date == measure_date,
            ).all()
            content_dup = False
            for old in same_content:
                if _fp_key(old.id_card, old.measure_time, old.grip_strength, old.chair_stand_time, old.walking_time, old.smi, old.systolic) == fkey:
                    content_dup = True
                    break
            if content_dup:
                skipped += 1
                messages.append(f"第 {idx+2} 列：與資料庫內容相同，已略過（{user_name}）")
                continue

            bmi = calc_bmi(height, weight)
            stage, abn_count, status_text = judge_sarcopenia(gender, grip, chair, walk, smi, age=age)
            if systolic and systolic >= int(th.get("systolic_high", 140)):
                if status_text == "各項指標正常":
                    status_text = f"血壓偏高 ({systolic}/{diastolic or '?'})"
                else:
                    status_text += f" / 血壓偏高 ({systolic}/{diastolic or '?'})"
                abn_count += 1

            rec = Measurement(
                id_card=id_card,
                user_name=user_name,
                gender=gender,
                age=age,
                height=height,
                weight=weight,
                bmi=bmi,
                body_fat=body_fat,
                smi=smi,
                systolic=systolic,
                diastolic=diastolic,
                pulse=pulse,
                grip_strength=grip,
                chair_stand_time=chair,
                walking_time=walk,
                sarcopenia_stage=stage,
                abnormal_count=abn_count,
                status=status_text,
                measure_date=measure_date,
                measure_time=measure_time,
                source="csv" if filename.endswith(".csv") else "excel",
                created_by=current_user.username,
            )
            db.add(rec)
            db.flush()
            create_abnormal_alert(db, rec)
            success += 1
        except Exception as e:
            failed += 1
            messages.append(f"第 {idx+2} 列錯誤：{e}")

    db.commit()
    deleted, del_names = purge_duplicate_measurements(db)
    if deleted:
        messages.append(f"自動審核後刪除資料庫重複 {deleted} 筆")
        messages.extend([f"已刪除：{n}" for n in del_names[:10]])
    add_audit(
        db, current_user.username, "import",
        f"file={filename} success={success} skipped={skipped} failed={failed} deleted={deleted}"
    )
    return ImportResult(success=success, skipped=skipped, failed=failed, deleted=deleted, messages=messages[:40])


@app.post("/api/dedupe", tags=["Import"])
def dedupe_measurements(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deleted, names = purge_duplicate_measurements(db)
    add_audit(db, current_user.username, "dedupe", f"deleted={deleted}")
    return {
        "deleted": deleted,
        "messages": [f"已刪除：{n}" for n in names] or ["沒有發現重複資料"],
    }


# ========== Stats ==========
@app.get("/api/stats", response_model=StatsOut, tags=["Stats"])
def get_stats(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Measurement)
    if start_date:
        query = query.filter(Measurement.measure_date >= start_date)
    if end_date:
        query = query.filter(Measurement.measure_date <= end_date)
    records = query.all()
    total = len(records)
    if total == 0:
        return StatsOut(
            total_records=0, unique_users=0, avg_grip=0, avg_chair=0, avg_walk=0,
            multi_abnormal_rate=0, summary_text="所選區間無檢測數據。",
            sarcopenia_pie=[
                {"name": "正常", "value": 0, "color": "#10b981"},
                {"name": "肌少症前期", "value": 0, "color": "#f59e0b"},
                {"name": "肌少症", "value": 0, "color": "#f97316"},
                {"name": "嚴重肌少症", "value": 0, "color": "#ef4444"},
            ],
            monthly_trends={"months": [], "counts": [], "avg_grip": [], "avg_chair": [], "avg_walk": []},
        )

    unique_users = len(set(r.id_card for r in records))
    grips = [r.grip_strength for r in records if r.grip_strength is not None]
    chairs = [r.chair_stand_time for r in records if r.chair_stand_time is not None]
    walks = [r.walking_time for r in records if r.walking_time is not None]
    multi = sum(1 for r in records if (r.abnormal_count or 0) >= 2)
    stage_counts = {"正常": 0, "肌少症前期": 0, "肌少症": 0, "嚴重肌少症": 0}
    month_map = {}
    for r in records:
        stage_counts[r.sarcopenia_stage] = stage_counts.get(r.sarcopenia_stage, 0) + 1
        mkey = (r.measure_date or "")[:7]
        if mkey:
            if mkey not in month_map:
                month_map[mkey] = {"count": 0, "grips": [], "chairs": [], "walks": []}
            month_map[mkey]["count"] += 1
            if r.grip_strength is not None:
                month_map[mkey]["grips"].append(r.grip_strength)
            if r.chair_stand_time is not None:
                month_map[mkey]["chairs"].append(r.chair_stand_time)
            if r.walking_time is not None:
                month_map[mkey]["walks"].append(r.walking_time)

    sorted_months = sorted(month_map.keys())
    monthly_trends = {
        "months": sorted_months,
        "counts": [month_map[m]["count"] for m in sorted_months],
        "avg_grip": [round(sum(month_map[m]["grips"]) / len(month_map[m]["grips"]), 1) if month_map[m]["grips"] else 0 for m in sorted_months],
        "avg_chair": [round(sum(month_map[m]["chairs"]) / len(month_map[m]["chairs"]), 1) if month_map[m]["chairs"] else 0 for m in sorted_months],
        "avg_walk": [round(sum(month_map[m]["walks"]) / len(month_map[m]["walks"]), 1) if month_map[m]["walks"] else 0 for m in sorted_months],
    }
    pre_pct = round(stage_counts.get("肌少症前期", 0) / total * 100, 1)
    sarco_pct = round((stage_counts.get("肌少症", 0) + stage_counts.get("嚴重肌少症", 0)) / total * 100, 1)
    return StatsOut(
        total_records=total,
        unique_users=unique_users,
        avg_grip=round(sum(grips) / len(grips), 1) if grips else 0,
        avg_chair=round(sum(chairs) / len(chairs), 1) if chairs else 0,
        avg_walk=round(sum(walks) / len(walks), 1) if walks else 0,
        multi_abnormal_rate=round(multi / total * 100, 1),
        summary_text=(
            f"指定統計區間累計完成 {total} 筆體適能與健康量測，列冊追蹤 {unique_users} 位長者。"
            f"【肌少衰弱分期】：判定為「肌少症前期」佔 {pre_pct}%，"
            f"進入「肌少症/嚴重肌少症」階段佔 {sarco_pct}%。"
        ),
        sarcopenia_pie=[
            {"name": "正常", "value": stage_counts.get("正常", 0), "color": "#10b981"},
            {"name": "肌少症前期", "value": stage_counts.get("肌少症前期", 0), "color": "#f59e0b"},
            {"name": "肌少症", "value": stage_counts.get("肌少症", 0), "color": "#f97316"},
            {"name": "嚴重肌少症", "value": stage_counts.get("嚴重肌少症", 0), "color": "#ef4444"},
        ],
        monthly_trends=monthly_trends,
    )


@app.get("/api/report/period", tags=["Report"])
def period_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Measurement)
    if start_date:
        query = query.filter(Measurement.measure_date >= start_date)
    if end_date:
        query = query.filter(Measurement.measure_date <= end_date)
    records = query.all()
    total = len(records)
    unique = len(set(r.id_card for r in records))
    stages = {}
    abnormal_people = set()
    for r in records:
        stages[r.sarcopenia_stage or "正常"] = stages.get(r.sarcopenia_stage or "正常", 0) + 1
        if (r.abnormal_count or 0) > 0 or (r.sarcopenia_stage and r.sarcopenia_stage != "正常"):
            abnormal_people.add(r.id_card)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_records": total,
        "unique_users": unique,
        "abnormal_users": len(abnormal_people),
        "stage_counts": stages,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "operator": current_user.username,
    }


# ========== Export ==========
@app.get("/api/export/csv", tags=["Export"])
def export_csv(
    q: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Measurement)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Measurement.id_card.ilike(like),
            Measurement.user_name.ilike(like),
        ))
    if start_date:
        query = query.filter(Measurement.measure_date >= start_date)
    if end_date:
        query = query.filter(Measurement.measure_date <= end_date)
    records = query.order_by(Measurement.measure_time.desc()).all()
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "身分證", "姓名", "性別", "年齡", "身高cm", "體重kg", "BMI", "體脂率%",
        "SMI", "收縮壓", "舒張壓", "脈搏", "握力kg", "五次坐站秒", "走路時間秒",
        "肌少症分期", "異常數", "狀態", "檢測日期", "檢測時間"
    ])
    for r in records:
        writer.writerow([
            r.id_card, r.user_name, r.gender, r.age, r.height, r.weight, r.bmi, r.body_fat,
            r.smi, r.systolic, r.diastolic, r.pulse, r.grip_strength, r.chair_stand_time,
            r.walking_time, r.sarcopenia_stage, r.abnormal_count, r.status,
            r.measure_date, r.measure_time,
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=babymomo_export.csv"},
    )


# ========== Cases 個案總覽 ==========
@app.get("/api/cases", tags=["Cases"])
def list_cases(
    q: Optional[str] = None,
    stage: Optional[str] = None,
    need_care: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """每位長者最新一筆 + 異常次數摘要"""
    subq = (
        db.query(
            Measurement.id_card,
            func.max(Measurement.measure_time).label("max_time"),
        )
        .group_by(Measurement.id_card)
        .subquery()
    )
    query = (
        db.query(Measurement)
        .join(subq, (Measurement.id_card == subq.c.id_card) & (Measurement.measure_time == subq.c.max_time))
    )
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Measurement.id_card.ilike(like),
            Measurement.user_name.ilike(like),
        ))
    if stage:
        query = query.filter(Measurement.sarcopenia_stage == stage)
    if need_care:
        query = query.filter(or_(
            Measurement.abnormal_count > 0,
            Measurement.sarcopenia_stage != "正常",
        ))

    all_latest = query.order_by(Measurement.measure_time.desc()).all()
    total = len(all_latest)
    items = all_latest[(page - 1) * page_size: page * page_size]

    result = []
    for r in items:
        total_recs = db.query(Measurement).filter(Measurement.id_card == r.id_card).count()
        abn_times = db.query(Measurement).filter(
            Measurement.id_card == r.id_card,
            or_(Measurement.abnormal_count > 0, Measurement.sarcopenia_stage != "正常"),
        ).count()
        open_alerts = db.query(Alert).filter(
            Alert.id_card == r.id_card,
            Alert.is_handled == False,
        ).count()
        result.append({
            "id_card": r.id_card,
            "user_name": r.user_name,
            "gender": r.gender,
            "age": r.age,
            "latest_stage": r.sarcopenia_stage,
            "latest_time": r.measure_time,
            "abnormal_count": r.abnormal_count,
            "total_records": total_recs,
            "abnormal_times": abn_times,
            "open_alerts": open_alerts,
            "grip_strength": r.grip_strength,
            "chair_stand_time": r.chair_stand_time,
            "walking_time": r.walking_time,
            "smi": r.smi,
            "systolic": r.systolic,
            "diastolic": r.diastolic,
            "bmi": r.bmi,
            "need_care": (r.abnormal_count or 0) > 0 or (r.sarcopenia_stage and r.sarcopenia_stage != "正常"),
        })
    return {"total": total, "page": page, "page_size": page_size, "items": result}


@app.get("/api/cases/{id_card}", tags=["Cases"])
def get_case(
    id_card: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    records = (
        db.query(Measurement)
        .filter(Measurement.id_card == id_card)
        .order_by(Measurement.measure_time.desc())
        .all()
    )
    if not records:
        raise HTTPException(404, "找不到此個案")
    latest = records[0]
    notes = (
        db.query(CareNote)
        .filter(CareNote.id_card == id_card)
        .order_by(CareNote.created_at.desc())
        .limit(50)
        .all()
    )
    advice = get_exercise_advice(latest.sarcopenia_stage)
    return {
        "profile": {
            "id_card": latest.id_card,
            "user_name": latest.user_name,
            "gender": latest.gender,
            "age": latest.age,
            "height": latest.height,
            "weight": latest.weight,
            "bmi": latest.bmi,
        },
        "latest": MeasurementOut.model_validate(latest),
        "total_records": len(records),
        "history": [MeasurementOut.model_validate(r) for r in records],
        "care_notes": [CareNoteOut.model_validate(n) for n in notes],
        "exercise_advice": advice,
        "suggested_retest_date": suggested_retest_date(
            latest.sarcopenia_stage, latest.measure_date
        ),
        "standards_compare": compare_to_standards(
            latest.age,
            latest.gender,
            weight=latest.weight,
            body_fat=latest.body_fat,
            smi=latest.smi,
            bmi=latest.bmi,
            systolic=latest.systolic,
            diastolic=latest.diastolic,
            pulse=latest.pulse,
            grip_strength=latest.grip_strength,
            chair_stand_time=latest.chair_stand_time,
            walking_time=latest.walking_time,
        ),
    }


@app.get("/api/cases/{id_card}/report", tags=["Report"], response_class=HTMLResponse)
def case_print_report(
    id_card: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    records = (
        db.query(Measurement)
        .filter(Measurement.id_card == id_card)
        .order_by(Measurement.measure_time.desc())
        .all()
    )
    if not records:
        raise HTTPException(404, "找不到此個案")
    latest = records[0]
    g = "男" if latest.gender == "M" else "女"
    advice_obj = get_exercise_advice(latest.sarcopenia_stage)
    retest = suggested_retest_date(latest.sarcopenia_stage, latest.measure_date)
    advice_items = "".join(f"<li>{it}</li>" for it in (advice_obj.get("items") or []))

    hist_rows = "".join(
        f"<tr><td>{r.measure_time or '-'}</td><td>{r.grip_strength if r.grip_strength is not None else '-'}</td>"
        f"<td>{r.chair_stand_time if r.chair_stand_time is not None else '-'}</td>"
        f"<td>{r.walking_time if r.walking_time is not None else '-'}</td>"
        f"<td>{r.smi if r.smi is not None else '-'}</td>"
        f"<td>{r.systolic or '-'}/{r.diastolic or '-'}</td>"
        f"<td>{r.sarcopenia_stage or '-'}</td></tr>"
        for r in records[:12]
    )
    html = f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8">
<title>個案報告 - {latest.user_name}</title>
<style>
body{{font-family:"Noto Sans TC",sans-serif;padding:24px;color:#111}}
h1{{font-size:20px;margin:0 0 8px}} h2{{font-size:16px;margin:20px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border:1px solid #ccc;padding:6px 8px;text-align:left}}
th{{background:#f1f5f9}} .meta{{color:#555;font-size:13px;margin-bottom:16px}}
.badge{{display:inline-block;padding:2px 8px;border-radius:4px;background:#e2e8f0}}
.advice{{background:#fef3c7;padding:12px;border-radius:8px;margin-top:12px}}
.advice ul{{margin:8px 0 0;padding-left:20px}}
@media print{{button{{display:none}}}}
</style></head><body>
<button onclick="window.print()">列印</button>
<h1>寶貝機 · 長者體適能檢測報告</h1>
<div class="meta">產生時間：{datetime.now().strftime("%Y-%m-%d %H:%M")}　操作者：{current_user.display_name}</div>
<h2>基本資料</h2>
<p><strong>{latest.user_name}</strong>（{latest.id_card}）　{g}　{latest.age or '-'} 歲<br>
身高 {latest.height or '-'} cm　體重 {latest.weight or '-'} kg　BMI {latest.bmi or '-'}<br>
最近檢測：{latest.measure_time or '-'}　分期：<span class="badge">{latest.sarcopenia_stage or '-'}</span></p>
<h2>最近指標</h2>
<table><tr><th>握力 kg</th><th>五次坐站 秒</th><th>走路時間 秒</th><th>SMI</th><th>血壓</th><th>脈搏</th></tr>
<tr><td>{latest.grip_strength if latest.grip_strength is not None else '-'}</td>
<td>{latest.chair_stand_time if latest.chair_stand_time is not None else '-'}</td>
<td>{latest.walking_time if latest.walking_time is not None else '-'}</td>
<td>{latest.smi if latest.smi is not None else '-'}</td>
<td>{latest.systolic or '-'}/{latest.diastolic or '-'}</td>
<td>{latest.pulse or '-'}</td></tr></table>
<p>說明：{latest.status or '-'}</p>
<div class="advice">
  <strong>{advice_obj.get('title', '建議')}</strong>：{advice_obj.get('summary', '')}
  <ul>{advice_items}</ul>
  <p style="margin:8px 0 0"><strong>建議複檢日期：</strong>{retest}</p>
</div>
<h2>歷史紀錄（最近 12 筆）</h2>
<table><tr><th>時間</th><th>握力</th><th>坐站</th><th>走路</th><th>SMI</th><th>血壓</th><th>分期</th></tr>
{hist_rows}</table>
<p style="margin-top:24px;font-size:12px;color:#888">本報告由寶貝機體適能檢測系統產生，僅供參考，不取代醫療診斷。</p>
</body></html>"""
    return HTMLResponse(html)


# ========== Case PDF Report ==========
@app.get("/api/cases/{id_card}/report.pdf", tags=["Report"])
def case_pdf_report(
    id_card: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """產生個案 PDF 報告（可下載存檔）。"""
    from fpdf import FPDF

    records = (
        db.query(Measurement)
        .filter(Measurement.id_card == id_card)
        .order_by(Measurement.measure_time.desc())
        .all()
    )
    if not records:
        raise HTTPException(404, "找不到此個案")
    latest = records[0]
    g = "男" if latest.gender == "M" else "女"
    advice = get_exercise_advice(latest.sarcopenia_stage)
    retest = suggested_retest_date(latest.sarcopenia_stage, latest.measure_date)

    font_path = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    if not os.path.isfile(font_path):
        # fallback common paths
        for p in (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "C:/Windows/Fonts/msjh.ttc",
            "C:/Windows/Fonts/mingliu.ttc",
        ):
            if os.path.isfile(p):
                font_path = p
                break

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    try:
        pdf.add_font("Noto", "", font_path)
        pdf.set_font("Noto", size=14)
    except Exception:
        pdf.set_font("Helvetica", size=14)

    def text(s, size=11):
        try:
            pdf.set_font("Noto", size=size)
        except Exception:
            pdf.set_font("Helvetica", size=size)
        pdf.multi_cell(0, 7, str(s) if s is not None else "-")

    text("寶貝機 · 長者體適能檢測報告", 16)
    text(f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}　操作者：{current_user.display_name}", 9)
    pdf.ln(2)
    text("【基本資料】", 12)
    text(
        f"{latest.user_name}（{latest.id_card}）　{g}　{latest.age or '-'} 歲\n"
        f"身高 {latest.height or '-'} cm　體重 {latest.weight or '-'} kg　BMI {latest.bmi or '-'}\n"
        f"最近檢測：{latest.measure_time or '-'}　分期：{latest.sarcopenia_stage or '-'}"
    )
    pdf.ln(1)
    text("【最近指標】", 12)
    text(
        f"握力：{latest.grip_strength if latest.grip_strength is not None else '-'} kg\n"
        f"五次坐站：{latest.chair_stand_time if latest.chair_stand_time is not None else '-'} 秒\n"
        f"走路時間：{latest.walking_time if latest.walking_time is not None else '-'} 秒\n"
        f"SMI：{latest.smi if latest.smi is not None else '-'}\n"
        f"血壓：{latest.systolic or '-'}/{latest.diastolic or '-'} mmHg　脈搏：{latest.pulse or '-'} bpm\n"
        f"說明：{latest.status or '-'}"
    )
    pdf.ln(1)
    text("【運動／復健建議】", 12)
    text(f"{advice.get('title', '')}：{advice.get('summary', '')}")
    for i, item in enumerate(advice.get("items") or [], 1):
        text(f"{i}. {item}", 10)
    text(f"建議複檢日期：{retest}", 11)
    pdf.ln(1)
    text("【歷史紀錄（最近 12 筆）】", 12)
    for r in records[:12]:
        text(
            f"{r.measure_time or '-'} | 握力 {r.grip_strength if r.grip_strength is not None else '-'} | "
            f"坐站 {r.chair_stand_time if r.chair_stand_time is not None else '-'} | "
            f"走路 {r.walking_time if r.walking_time is not None else '-'} | "
            f"SMI {r.smi if r.smi is not None else '-'} | "
            f"{r.systolic or '-'}/{r.diastolic or '-'} | {r.sarcopenia_stage or '-'}",
            9,
        )
    pdf.ln(3)
    text("本報告由寶貝機體適能檢測系統產生，僅供參考，不取代醫療診斷。", 8)

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    fname = f"babymomo_{latest.id_card}_{datetime.now().strftime('%Y%m%d')}.pdf"
    add_audit(db, current_user.username, "export_pdf", f"個案 {latest.user_name}({id_card})")
    db.commit()
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ========== 複檢提醒 ==========
@app.get("/api/reminders", tags=["Care"])
def list_reminders(
    days: int = Query(14, ge=1, le=90),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """列出即將到期或已過期的複檢／追蹤提醒。"""
    today = datetime.now().strftime("%Y-%m-%d")
    from datetime import timedelta
    end = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    notes = (
        db.query(CareNote)
        .filter(
            CareNote.next_follow_date.isnot(None),
            CareNote.next_follow_date != "",
            CareNote.next_follow_date <= end,
        )
        .order_by(CareNote.next_follow_date.asc())
        .limit(200)
        .all()
    )
    # 去重：同一身分證只留最近一筆有 next_follow_date 的
    seen = set()
    items = []
    for n in notes:
        if n.id_card in seen:
            continue
        seen.add(n.id_card)
        overdue = (n.next_follow_date or "") < today
        # 取最新分期
        latest = (
            db.query(Measurement)
            .filter(Measurement.id_card == n.id_card)
            .order_by(Measurement.measure_time.desc())
            .first()
        )
        items.append({
            "id": n.id,
            "id_card": n.id_card,
            "user_name": n.user_name or (latest.user_name if latest else None),
            "next_follow_date": n.next_follow_date,
            "overdue": overdue,
            "content": n.content,
            "created_by": n.created_by,
            "stage": latest.sarcopenia_stage if latest else None,
            "suggested_retest_date": suggested_retest_date(
                latest.sarcopenia_stage if latest else None,
                latest.measure_date if latest else None,
            ),
        })
    # 也納入「有異常但沒有任何 next_follow_date」的個案（依分期建議複檢日）
    subq = (
        db.query(
            Measurement.id_card,
            func.max(Measurement.measure_time).label("max_time"),
        )
        .group_by(Measurement.id_card)
        .subquery()
    )
    abnormal_latest = (
        db.query(Measurement)
        .join(subq, (Measurement.id_card == subq.c.id_card) & (Measurement.measure_time == subq.c.max_time))
        .filter(or_(
            Measurement.abnormal_count > 0,
            Measurement.sarcopenia_stage != "正常",
        ))
        .all()
    )
    for r in abnormal_latest:
        if r.id_card in seen:
            continue
        sug = suggested_retest_date(r.sarcopenia_stage, r.measure_date)
        if sug <= end:
            seen.add(r.id_card)
            items.append({
                "id": None,
                "id_card": r.id_card,
                "user_name": r.user_name,
                "next_follow_date": sug,
                "overdue": sug < today,
                "content": "系統依分期自動建議複檢（尚未建立關懷紀錄）",
                "created_by": "system",
                "stage": r.sarcopenia_stage,
                "suggested_retest_date": sug,
            })
    items.sort(key=lambda x: (x["next_follow_date"] or "9999", x["id_card"]))
    return {
        "today": today,
        "until": end,
        "total": len(items),
        "overdue_count": sum(1 for i in items if i["overdue"]),
        "items": items,
    }


# ========== 資料庫備份 ==========
@app.get("/api/admin/backup", tags=["System"])
def download_backup(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("superadmin", "admin")),
):
    """一鍵下載 SQLite 資料庫備份（僅管理員）。"""
    db_url = os.getenv("DATABASE_URL", "sqlite:///./babymomo.db")
    if not db_url.startswith("sqlite"):
        raise HTTPException(400, "目前僅支援 SQLite 一鍵備份。PostgreSQL 請使用平台備份工具。")
    # 解析路徑
    path = db_url.replace("sqlite:///", "", 1)
    if path.startswith("./"):
        path = os.path.join(os.path.dirname(__file__), path[2:])
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)
    if not os.path.isfile(path):
        raise HTTPException(404, f"找不到資料庫檔案：{path}")
    add_audit(db, current_user.username, "backup_db", path)
    db.commit()
    fname = f"babymomo_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=fname,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ========== Care notes ==========
@app.get("/api/care-notes", tags=["Care"])
def list_care_notes(
    id_card: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(CareNote)
    if id_card:
        q = q.filter(CareNote.id_card == id_card)
    total = q.count()
    items = q.order_by(CareNote.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [CareNoteOut.model_validate(i) for i in items]}


@app.post("/api/care-notes", response_model=CareNoteOut, tags=["Care"])
def create_care_note(
    payload: CareNoteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    name = None
    latest = (
        db.query(Measurement)
        .filter(Measurement.id_card == payload.id_card)
        .order_by(Measurement.measure_time.desc())
        .first()
    )
    if latest:
        name = latest.user_name
    note = CareNote(
        id_card=payload.id_card,
        user_name=name,
        alert_id=payload.alert_id,
        note_type=payload.note_type or "followup",
        content=payload.content,
        next_follow_date=payload.next_follow_date,
        created_by=current_user.username,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    add_audit(db, current_user.username, "care_note", f"{payload.id_card} {payload.content[:40]}")
    return note


# ========== Alerts ==========
@app.get("/api/alerts", tags=["Alerts"])
def list_alerts(
    only_unread: bool = False,
    only_unhandled: bool = False,
    q: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    stage: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Alert)
    role = current_user.role or "staff"
    if role not in ("superadmin", "admin", "company_admin"):
        query = query.filter(Alert.target_roles.ilike(f"%{role}%"))
    if only_unread:
        query = query.filter(Alert.is_read == False)
    if only_unhandled:
        query = query.filter(Alert.is_handled == False)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Alert.id_card.ilike(like), Alert.user_name.ilike(like)))
    if start_date:
        # created_at is datetime; compare date prefix
        query = query.filter(Alert.created_at >= start_date)
    if end_date:
        query = query.filter(Alert.created_at <= end_date + " 23:59:59")
    if stage:
        query = query.filter(Alert.sarcopenia_stage == stage)
    total = query.count()
    items = query.order_by(Alert.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "unread": db.query(Alert).filter(Alert.is_read == False).count(),
        "unhandled": db.query(Alert).filter(Alert.is_handled == False).count(),
        "page": page,
        "items": [
            _alert_item_dict(db, a)
            for a in items
        ],
    }


def _vitals_for_alert(db: Session, a: Alert) -> dict:
    rec = None
    if a.measurement_id:
        rec = db.query(Measurement).filter(Measurement.id == a.measurement_id).first()
    if not rec:
        rec = (
            db.query(Measurement)
            .filter(Measurement.id_card == a.id_card)
            .order_by(Measurement.measure_time.desc())
            .first()
        )
    if not rec:
        return {}
    return {
        "gender": rec.gender,
        "age": rec.age,
        "height": rec.height,
        "weight": rec.weight,
        "bmi": rec.bmi,
        "body_fat": rec.body_fat,
        "smi": rec.smi,
        "systolic": rec.systolic,
        "diastolic": rec.diastolic,
        "pulse": rec.pulse,
        "grip_strength": rec.grip_strength,
        "chair_stand_time": rec.chair_stand_time,
        "walking_time": rec.walking_time,
        "bp_status": bp_status(rec.systolic, rec.diastolic),
        "measure_time": rec.measure_time,
    }


@app.post("/api/alerts/{alert_id}/read", tags=["Alerts"])
def mark_alert_read(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    a = db.query(Alert).get(alert_id)
    if not a:
        raise HTTPException(404, "找不到通報")
    a.is_read = True
    a.read_by = current_user.username
    a.read_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True}


@app.post("/api/alerts/{alert_id}/handle", tags=["Alerts"])
def handle_alert(
    alert_id: int,
    note: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    a = db.query(Alert).get(alert_id)
    if not a:
        raise HTTPException(404, "找不到通報")
    a.is_read = True
    a.is_handled = True
    a.handled_by = current_user.username
    a.handled_at = datetime.now(timezone.utc)
    a.handle_note = note or "已關懷處理"
    if not a.read_by:
        a.read_by = current_user.username
        a.read_at = a.handled_at
    db.commit()
    # 同步寫一筆關懷紀錄
    db.add(CareNote(
        id_card=a.id_card,
        user_name=a.user_name,
        alert_id=a.id,
        note_type="followup",
        content=a.handle_note,
        created_by=current_user.username,
    ))
    db.commit()
    add_audit(db, current_user.username, "handle_alert", f"alert={alert_id} {a.user_name}")
    return {"ok": True}


@app.post("/api/alerts/batch-handle", tags=["Alerts"])
def batch_handle_alerts(
    ids: List[int] = Body(...),
    note: str = Body("批次標記已關懷"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not ids:
        raise HTTPException(400, "請提供通報 id 列表")
    now = datetime.now(timezone.utc)
    count = 0
    for aid in ids:
        a = db.query(Alert).get(aid)
        if not a or a.is_handled:
            continue
        a.is_read = True
        a.is_handled = True
        a.handled_by = current_user.username
        a.handled_at = now
        a.handle_note = note
        db.add(CareNote(
            id_card=a.id_card,
            user_name=a.user_name,
            alert_id=a.id,
            note_type="followup",
            content=note,
            created_by=current_user.username,
        ))
        count += 1
    db.commit()
    add_audit(db, current_user.username, "batch_handle_alerts", f"count={count}")
    return {"ok": True, "handled": count}


@app.post("/api/alerts/{alert_id}/line", tags=["Alerts"])
def send_alert_to_line(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    a = db.query(Alert).get(alert_id)
    if not a:
        raise HTTPException(404, "找不到通報")
    result = send_line_text(a.message or a.title)
    add_audit(db, current_user.username, "line_alert", f"alert={alert_id} ok={result.get('ok')}")
    return result


@app.post("/api/alerts/line-test", tags=["Alerts"])
def send_line_test(current_user: User = Depends(get_current_user)):
    sample = (
        "【寶貝機模擬通報】\n"
        "個案：王小明（A123456789）\n"
        "性別/年齡：男 / 72 歲\n"
        "檢測時間：2026-09-15 10:00:00\n"
        "握力：16.2 kg（不足）\n"
        "五次坐站：14.8 秒（偏慢）\n"
        "走路時間：22.1 秒（偏慢）\n"
        "SMI：6.4（偏低）\n"
        "血壓：168/98 mmHg（血壓偏高）\n"
        "肌少症分期：嚴重肌少症\n"
        "建議：盡快關懷，必要時轉介醫療或安排兩週內複測。\n"
        f"操作者：{current_user.username}"
    )
    result = send_line_text(sample)
    result["configured"] = line_configured()
    return result


@app.post("/api/alerts/daily-summary", tags=["Alerts"])
def send_daily_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    today = datetime.now().strftime("%Y-%m-%d")
    today_recs = db.query(Measurement).filter(Measurement.measure_date == today).count()
    unhandled = db.query(Alert).filter(Alert.is_handled == False).count()
    today_alerts = db.query(Alert).filter(Alert.created_at >= datetime.now().replace(hour=0, minute=0, second=0)).count()
    text = (
        f"【寶貝機每日摘要】{today}\n"
        f"今日新增檢測：{today_recs} 筆\n"
        f"今日新增異常通報：{today_alerts} 筆\n"
        f"目前未處理通報：{unhandled} 筆\n"
        f"請登入系統查看詳情。"
    )
    result = send_line_text(text)
    add_audit(db, current_user.username, "daily_summary", f"ok={result.get('ok')}")
    return result


# ========== Thresholds ==========
@app.get("/api/settings/thresholds", response_model=ThresholdsOut, tags=["Settings"])
def get_threshold_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return ThresholdsOut(**get_thresholds(db))


@app.put("/api/settings/thresholds", response_model=ThresholdsOut, tags=["Settings"])
def update_threshold_settings(
    payload: ThresholdsOut,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    data = payload.model_dump()
    row = db.query(SystemConfig).filter(SystemConfig.key == "thresholds").first()
    if row:
        row.value = json.dumps(data)
        row.updated_by = current_user.username
    else:
        db.add(SystemConfig(key="thresholds", value=json.dumps(data), updated_by=current_user.username))
    db.commit()
    add_audit(db, current_user.username, "update_thresholds", json.dumps(data))
    return payload


@app.get("/api/settings/equipment", tags=["Settings"])
def get_equipment_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取得真茂科技運動輔具建議目錄（所有登入者可讀，管理員可改）。"""
    return {"items": get_equipment_catalog(db)}


@app.put("/api/settings/equipment", tags=["Settings"])
def update_equipment_settings(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    """管理員編輯運動輔具建議文字與觸發條件。"""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or len(items) == 0:
        raise HTTPException(400, "請提供至少一筆輔具建議")
    cleaned = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        triggers = it.get("triggers") or ["always_abnormal"]
        if not isinstance(triggers, list):
            triggers = ["always_abnormal"]
        cleaned.append({
            "id": str(it.get("id") or f"eq_{i+1}"),
            "name": name[:100],
            "why": str(it.get("why") or "")[:500],
            "how": str(it.get("how") or "")[:800],
            "caution": str(it.get("caution") or "")[:400],
            "triggers": [str(t) for t in triggers][:10],
        })
    if not cleaned:
        raise HTTPException(400, "沒有有效的輔具項目")
    row = db.query(SystemConfig).filter(SystemConfig.key == "equipment_catalog").first()
    value = json.dumps(cleaned, ensure_ascii=False)
    if row:
        row.value = value
        row.updated_by = current_user.username
    else:
        db.add(SystemConfig(key="equipment_catalog", value=value, updated_by=current_user.username))
    db.commit()
    add_audit(db, current_user.username, "update_equipment", f"count={len(cleaned)}")
    return {"ok": True, "items": cleaned}


@app.post("/api/settings/equipment/reset", tags=["Settings"])
def reset_equipment_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    """還原為系統預設輔具建議。"""
    defaults = get_default_equipment_catalog()
    row = db.query(SystemConfig).filter(SystemConfig.key == "equipment_catalog").first()
    value = json.dumps(defaults, ensure_ascii=False)
    if row:
        row.value = value
        row.updated_by = current_user.username
    else:
        db.add(SystemConfig(key="equipment_catalog", value=value, updated_by=current_user.username))
    db.commit()
    add_audit(db, current_user.username, "reset_equipment", "defaults")
    return {"ok": True, "items": defaults}


# ========== Audit logs ==========
@app.get("/api/audit-logs", tags=["Audit"])
def list_audit_logs(
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    query = db.query(AuditLog)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            AuditLog.operator.ilike(like),
            AuditLog.action.ilike(like),
            AuditLog.details.ilike(like),
        ))
    total = query.count()
    items = query.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "items": [
            {
                "id": a.id,
                "operator": a.operator,
                "action": a.action,
                "details": a.details,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in items
        ],
    }




@app.get("/api/standards", tags=["Standards"])
def api_standards(
    age: int = Query(..., ge=50, le=120),
    gender: str = Query(..., description="M/F 或 男/女"),
    current_user: User = Depends(get_current_user),
):
    """查詢指定年齡性別的標準值表。"""
    from standards import get_standards
    std = get_standards(age, gender)
    if not std:
        raise HTTPException(404, "找不到對應標準值")
    return std


@app.post("/api/feedback", tags=["Feedback"])
def submit_feedback(
    payload: FeedbackCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """意見／錯誤回饋：寫入資料庫並推播到既有官方 LINE。"""
    cat = (payload.category or "suggestion").strip().lower()
    if cat not in ("suggestion", "bug", "other"):
        cat = "other"
    cat_label = {"suggestion": "意見建議", "bug": "錯誤回報", "other": "其他"}.get(cat, "其他")

    fb = Feedback(
        category=cat,
        title=(payload.title or "").strip() or None,
        content=payload.content.strip(),
        contact=(payload.contact or "").strip() or None,
        page_url=(payload.page_url or "").strip() or None,
        created_by=current_user.username,
        created_by_name=current_user.display_name or current_user.username,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)

    lines = [
        f"【寶貝機 · {cat_label}】",
        f"回報者：{fb.created_by_name}（{fb.created_by}）",
    ]
    if fb.title:
        lines.append(f"標題：{fb.title}")
    lines.append(f"內容：\n{fb.content}")
    if fb.contact:
        lines.append(f"聯絡方式：{fb.contact}")
    if fb.page_url:
        lines.append(f"頁面：{fb.page_url}")
    lines.append(f"時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    text = "\n".join(lines)

    result = send_line_text(text)
    fb.line_sent = bool(result.get("ok"))
    fb.line_result = json.dumps(result, ensure_ascii=False)[:2000]
    db.commit()

    add_audit(
        db,
        current_user.username,
        "feedback",
        f"id={fb.id} cat={cat} line_ok={result.get('ok')}",
    )
    return {
        "ok": True,
        "id": fb.id,
        "line_sent": fb.line_sent,
        "line": result,
        "message": "已送出回饋" + ("，並已推播至 LINE" if fb.line_sent else "（LINE 推播未成功，已存檔）"),
    }


@app.get("/api/feedback", tags=["Feedback"])
def list_feedback(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    category: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("superadmin", "admin")),
):
    q = db.query(Feedback)
    if category:
        q = q.filter(Feedback.category == category)
    total = q.count()
    rows = (
        q.order_by(desc(Feedback.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": r.id,
                "category": r.category,
                "title": r.title,
                "content": r.content,
                "contact": r.contact,
                "page_url": r.page_url,
                "created_by": r.created_by,
                "created_by_name": r.created_by_name,
                "line_sent": r.line_sent,
                "is_handled": r.is_handled,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@app.get("/api/health", tags=["System"])
def health():
    return {"status": "ok", "service": "寶貝機體適能檢測系統", "version": "2.0.0"}


# Frontend static
_base = os.path.dirname(__file__)
_candidates = [
    _base,
    os.path.join(_base, "frontend"),
    os.path.join(_base, "..", "frontend"),
]
frontend_path = None
for p in _candidates:
    if os.path.isfile(os.path.join(p, "index.html")):
        frontend_path = p
        break


@app.get("/")
def home_page():
    if frontend_path:
        return FileResponse(os.path.join(frontend_path, "index.html"))
    return {"detail": "Frontend not found. Upload index.html into backend folder."}


if frontend_path:
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
