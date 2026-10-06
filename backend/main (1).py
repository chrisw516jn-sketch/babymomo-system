from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query, status, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, desc, text
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
from models import User, Measurement, AuditLog, Alert, CareNote, SystemConfig
try:
    from models import Feedback
except Exception:
    Feedback = None
try:
    from models import CaseEquipmentPlan
except Exception:
    CaseEquipmentPlan = None
try:
    from models import SystemIssue
except Exception:
    SystemIssue = None
from schemas import (
    UserCreate, UserUpdate, UserOut, Token, MeasurementCreate, MeasurementOut,
    StatsOut, ImportResult, CareNoteCreate, CareNoteOut, ThresholdsOut,
)
try:
    from schemas import FeedbackCreate, FeedbackOut
except Exception:
    FeedbackCreate = None
    FeedbackOut = None
from auth import (
    get_password_hash, verify_password, create_access_token,
    get_current_user, require_roles, get_user_by_username, ACCESS_TOKEN_EXPIRE_MINUTES,
)
from utils import (
    calc_bmi, judge_sarcopenia, normalize_measure_time, format_alert_message,
    bp_status, get_exercise_advice, suggested_retest_date, get_intervention_plan,
)
try:
    from utils import get_default_equipment_catalog
except Exception:
    def get_default_equipment_catalog():
        return []
from line_notify import send_line_text, line_configured
try:
    from standards import compare_to_standards, walking_threshold
except Exception:
    def walking_threshold(*args, **kwargs):
        return 20.0
    def compare_to_standards(*args, **kwargs):
        return {}

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
    """讀取「預設範本」運動輔具建議目錄（非個案專用）。"""
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


def get_case_equipment_items(db: Session, id_card: str) -> Optional[list]:
    """取得個案客製化輔具建議；若無則回傳 None（改用系統自動）。"""
    if not id_card or CaseEquipmentPlan is None:
        return None
    row = db.query(CaseEquipmentPlan).filter(CaseEquipmentPlan.id_card == id_card).first()
    if not row or not row.items_json:
        return None
    try:
        data = json.loads(row.items_json)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return None


def build_intervention_for_case(db: Session, id_card: str, vitals: dict, stage: Optional[str] = None) -> dict:
    """優先使用個案客製輔具；沒有才用預設範本＋條件觸發。"""
    custom = get_case_equipment_items(db, id_card)
    plan = get_intervention_plan(
        stage=stage or vitals.get("sarcopenia_stage"),
        grip=vitals.get("grip_strength"),
        chair=vitals.get("chair_stand_time"),
        walk=vitals.get("walking_time"),
        smi=vitals.get("smi"),
        gender=vitals.get("gender"),
        bp_status_text=vitals.get("bp_status"),
        equipment_catalog=get_equipment_catalog(db),
    )
    if custom is not None:
        plan["equipment"] = [
            {
                "id": it.get("id") or "",
                "name": it.get("name") or "",
                "why": it.get("why") or "",
                "how": it.get("how") or "",
                "caution": it.get("caution") or "",
            }
            for it in custom
            if isinstance(it, dict) and (it.get("name") or "").strip()
        ]
        plan["equipment_source"] = "custom"
        plan["brand_note"] = "此為「個案客製化」運動輔具建議，由管理員依個人狀況編輯。"
    else:
        plan["equipment_source"] = "auto"
    return plan


def _alert_item_dict(db: Session, a: Alert) -> dict:
    vitals = _vitals_for_alert(db, a)
    plan = build_intervention_for_case(
        db,
        a.id_card,
        vitals,
        stage=a.sarcopenia_stage or vitals.get("sarcopenia_stage"),
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
    try:
        with engine.begin() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(measurements)"))]
            if "is_deleted" not in cols:
                conn.execute(text("ALTER TABLE measurements ADD COLUMN is_deleted BOOLEAN DEFAULT 0"))
            if "deleted_at" not in cols:
                conn.execute(text("ALTER TABLE measurements ADD COLUMN deleted_at DATETIME"))
            if "deleted_by" not in cols:
                conn.execute(text("ALTER TABLE measurements ADD COLUMN deleted_by VARCHAR(50)"))
    except Exception:
        pass
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
        # 啟動時若超過 24 小時未健檢則自動執行
        try:
            maybe_daily_self_heal(db)
        except Exception:
            pass
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
def _netown_msg(code: str, http_status: int = 200):
    return JSONResponse(status_code=http_status, content={"MessageCode": str(code)})


def _apply_netown_fields(rec: Measurement, fields: dict):
    """把 Netown 解析出的欄位寫入既有/新建 Measurement。"""
    mapping = [
        "height", "weight", "bmi", "body_fat", "smi",
        "systolic", "diastolic", "pulse",
        "grip_strength", "chair_stand_time", "walking_time",
    ]
    for k in mapping:
        if k in fields and fields[k] is not None:
            setattr(rec, k, fields[k])


def _recalc_measurement(db: Session, rec: Measurement):
    if (rec.bmi is None or rec.bmi == 0) and rec.height and rec.weight:
        rec.bmi = calc_bmi(rec.height, rec.weight)
    stage, abn_count, status_text = judge_sarcopenia(
        rec.gender, rec.grip_strength, rec.chair_stand_time,
        rec.walking_time, rec.smi, age=rec.age,
    )
    th = get_thresholds(db)
    if rec.systolic and rec.systolic >= int(th.get("systolic_high", 140)):
        if status_text == "各項指標正常":
            status_text = f"血壓偏高 ({rec.systolic}/{rec.diastolic or '?'})"
        else:
            status_text += f" / 血壓偏高 ({rec.systolic}/{rec.diastolic or '?'})"
        abn_count += 1
    rec.sarcopenia_stage = stage
    rec.abnormal_count = abn_count
    rec.status = status_text


def handle_netown_upload(body: dict, db: Session, headers: dict) -> JSONResponse:
    """處理 NETOWN API v2.4 設備上傳。"""
    try:
        from netown import is_netown_payload, parse_netown, netown_api_key_ok
    except Exception as e:
        return _netown_msg("500", 500)

    if not netown_api_key_ok(dict(headers)):
        return _netown_msg("401", 401)

    try:
        parsed = parse_netown(body)
    except ValueError:
        return _netown_msg("400", 400)
    except Exception:
        return _netown_msg("500", 500)

    id_card = parsed["id_card"]
    measure_date = parsed["measure_date"]
    measure_time = parsed["measure_time"]
    typ = parsed["type"]
    machine = parsed.get("machine_number") or ""

    try:
        # 同日合併：找同一身分證當日最新一筆，沒有就新建
        rec = (
            db.query(Measurement)
            .filter(Measurement.id_card == id_card, Measurement.measure_date == measure_date)
            .order_by(Measurement.measure_time.desc())
            .first()
        )
        if not rec:
            prev = (
                db.query(Measurement)
                .filter(Measurement.id_card == id_card)
                .order_by(Measurement.measure_time.desc())
                .first()
            )
            rec = Measurement(
                id_card=id_card,
                user_name=(prev.user_name if prev else id_card),
                gender=(prev.gender if prev and prev.gender else "M"),
                age=(prev.age if prev else None),
                measure_date=measure_date,
                measure_time=measure_time,
                source="netown",
                created_by=f"netown:{machine}"[:50],
            )
            db.add(rec)
            db.flush()
        else:
            # 更新為較新的量測時間
            if measure_time > (rec.measure_time or ""):
                rec.measure_time = measure_time

        if parsed.get("is_vital") and parsed.get("fields"):
            _apply_netown_fields(rec, parsed["fields"])
            _recalc_measurement(db, rec)

        # 運動摘要附加到 status（不覆蓋分期說明主體）
        summary = parsed.get("exercise_summary") or ""
        if summary:
            extra = f"[Netown/{typ}] {summary}"
            if rec.status and extra not in (rec.status or ""):
                rec.status = ((rec.status or "") + " | " + extra)[:500]
            elif not rec.status:
                rec.status = extra[:500]
            if not parsed.get("is_vital"):
                # 純運動紀錄仍確保有分期欄位
                if not rec.sarcopenia_stage:
                    _recalc_measurement(db, rec)

        db.commit()
        db.refresh(rec)

        if parsed.get("is_vital"):
            try:
                create_abnormal_alert(db, rec)
            except Exception:
                pass

        add_audit(
            db,
            f"netown:{machine}"[:50],
            "netown_upload",
            f"{id_card} type={typ} time={measure_time}",
        )
        return _netown_msg("200", 200)
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        return _netown_msg("500", 500)


@app.post("/api/measurements", tags=["Measurements"])
async def create_measurement(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    雙格式：
    1) NETOWN 設備：{ID, MachineNumber, Version, Type, MeasureTime, Values}
       → 回傳 {MessageCode: 200/400/401/500}，不需登入
    2) 系統內部：MeasurementCreate JSON + Bearer Token
       → 回傳 MeasurementOut
    """
    try:
        body = await request.json()
    except Exception:
        # 非 JSON：若像 Netown 則 400，否則 422
        return _netown_msg("400", 400)

    try:
        from netown import is_netown_payload
        netown_shape = is_netown_payload(body)
    except Exception:
        netown_shape = False

    if netown_shape:
        return handle_netown_upload(body, db, request.headers)

    # ----- 內部 API（需登入）-----
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "需要登入")
    token = auth[7:].strip()
    current_user = None
    try:
        from jose import jwt as jose_jwt
        from auth import SECRET_KEY, ALGORITHM
        payload_tok = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload_tok.get("sub")
        current_user = get_user_by_username(db, username) if username else None
    except Exception:
        current_user = None
    if current_user is None or not getattr(current_user, "is_active", True):
        raise HTTPException(401, "登入已過期或無效")

    try:
        payload = MeasurementCreate(**body)
    except Exception as e:
        raise HTTPException(422, f"欄位錯誤：{e}")

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


@app.post("/api/netown", tags=["Netown"])
async def netown_endpoint(request: Request, db: Session = Depends(get_db)):
    """NETOWN 專用別名（與 /api/measurements 相同邏輯）。"""
    try:
        body = await request.json()
    except Exception:
        return _netown_msg("400", 400)
    return handle_netown_upload(body, db, request.headers)


def _not_deleted():
    return or_(Measurement.is_deleted == False, Measurement.is_deleted.is_(None))


def _same_day_latest_ids(db: Session) -> set:
    """同一身分證同一天只保留時間最新的一筆（不含垃圾桶）。"""
    rows = db.query(
        Measurement.id, Measurement.id_card, Measurement.measure_date, Measurement.measure_time
    ).filter(_not_deleted()).all()
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
    query = db.query(Measurement).filter(_not_deleted())
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
    query = db.query(Measurement).filter(_not_deleted())
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
    """移到垃圾桶，不立刻消滅。"""
    rec = db.query(Measurement).get(record_id)
    if not rec:
        raise HTTPException(404, "找不到紀錄")
    rec.is_deleted = True
    rec.deleted_at = datetime.now(timezone.utc)
    rec.deleted_by = current_user.username
    db.query(Alert).filter(Alert.measurement_id == rec.id, Alert.is_handled == False).update(
        {"is_handled": True, "handle_note": "紀錄已移入垃圾桶"}, synchronize_session=False
    )
    db.commit()
    add_audit(db, current_user.username, "trash_measurement", f"id={record_id} {rec.user_name}")
    return {"ok": True, "trashed": True}


@app.get("/api/trash", tags=["Trash"])
def list_trash(
    q: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Measurement).filter(Measurement.is_deleted == True)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Measurement.id_card.ilike(like), Measurement.user_name.ilike(like)))
    total = query.count()
    items = query.order_by(Measurement.deleted_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": i.id,
                "id_card": i.id_card,
                "user_name": i.user_name,
                "measure_time": i.measure_time,
                "grip_strength": i.grip_strength,
                "chair_stand_time": i.chair_stand_time,
                "walking_time": i.walking_time,
                "smi": i.smi,
                "sarcopenia_stage": i.sarcopenia_stage,
                "deleted_by": i.deleted_by,
                "deleted_at": i.deleted_at.isoformat() if i.deleted_at else None,
                "source": i.source,
            }
            for i in items
        ],
    }


@app.post("/api/trash/{record_id}/restore", tags=["Trash"])
def restore_trash(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin", "company_admin")),
):
    rec = db.query(Measurement).get(record_id)
    if not rec or not rec.is_deleted:
        raise HTTPException(404, "垃圾桶找不到這筆")
    rec.is_deleted = False
    rec.deleted_at = None
    rec.deleted_by = None
    db.commit()
    add_audit(db, current_user.username, "restore_measurement", f"id={record_id} {rec.user_name}")
    return {"ok": True}


@app.delete("/api/trash/{record_id}", tags=["Trash"])
def purge_trash(
    record_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin", "company_admin")),
):
    rec = db.query(Measurement).get(record_id)
    if not rec or not rec.is_deleted:
        raise HTTPException(404, "垃圾桶找不到這筆")
    name = rec.user_name
    db.delete(rec)
    db.commit()
    add_audit(db, current_user.username, "purge_measurement", f"id={record_id} {name}")
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
        "id_card": ["id_card", "身分證", "身分證字號", "身分證號", "id", "idcard"],
        "user_name": ["user_name", "姓名", "name", "長者姓名", "個案姓名"],
        "gender": ["gender", "性別", "sex"],
        "age": ["age", "年齡"],
        "height": ["height", "身高", "身高cm", "身高(cm)"],
        "weight": ["weight", "體重", "體重kg", "體重(kg)"],
        "body_fat": ["body_fat", "體脂", "體脂率", "體脂肪", "體脂肪率", "脂肪"],
        "smi": ["smi", "骨骼肌指數", "smi_kgm2", "骨骼肌量smi"],
        "muscle_mass": ["肌肉質量", "肌肉量", "骨骼肌量", "除脂肪量"],
        "systolic": ["systolic", "收縮壓", "sbp", "收縮"],
        "diastolic": ["diastolic", "舒張壓", "dbp", "舒張"],
        "pulse": ["pulse", "脈搏", "心率", "hr"],
        "grip_strength": ["grip_strength", "握力", "握力kg", "握力(kg)"],
        "chair_stand_time": ["chair_stand_time", "五次坐站", "坐站", "chair", "5次坐站", "五次坐立"],
        "walking_time": ["walking_time", "走路時間", "步速", "walk", "起身行走", "TUG"],
        # 真正的日期欄優先；「檢測時間」在貴司 Excel 常是秒數，勿當日期
        "measure_time": [
            "日期", "檢測日期", "量測日期", "測量日期", "資料日期",
            "檢驗日期", "評估日期", "施測日期", "datetime", "date",
            "measure_date", "measured_at", "measure_time",
            "量測時間", "測量時間", "檢測日時",
        ],
    }
    sample_keys = list(rows[0].keys())
    lower_cols = {str(c).strip().lower(): str(c).strip() for c in sample_keys}
    # 也支援欄名含「日期」「時間」的模糊對應
    resolved = {}
    for key, aliases in col_map.items():
        for a in aliases:
            if a.lower() in lower_cols:
                resolved[key] = lower_cols[a.lower()]
                break
    if "measure_time" not in resolved:
        # 從樣本判斷哪個欄位看起來像日期時間，避開「檢測時間=10」這種秒數欄
        skip_names = ("走路時間", "坐站", "五次", "握力", "脈搏", "收縮", "舒張")
        candidates = []
        for lk, orig in lower_cols.items():
            if any(s in orig for s in skip_names):
                continue
            if any(k in lk for k in ("日期", "date", "datetime", "日時")):
                candidates.append(orig)
        if not candidates:
            for lk, orig in lower_cols.items():
                if orig in skip_names or "走路" in orig or "坐站" in orig:
                    continue
                if any(k in lk for k in ("時間", "time")) and "檢測時間" not in orig:
                    candidates.append(orig)
        if candidates:
            resolved["measure_time"] = candidates[0]
        else:
            # 最後才考慮「檢測時間」，且樣本必須像日期
            for lk, orig in lower_cols.items():
                if "檢測時間" in orig or lk == "time":
                    sample = rows[0].get(orig)
                    md, mt = normalize_measure_time(sample)
                    if sample is not None and str(sample).strip() and not (
                        isinstance(sample, (int, float)) and float(sample) < 40000
                    ) and md and not md.startswith(datetime.now().strftime("%Y-%m-%d")):
                        resolved["measure_time"] = orig
                    break
    if "id_card" not in resolved or "user_name" not in resolved:
        raise HTTPException(400, "檔案必須至少包含「身分證」與「姓名」欄位")

    success = skipped = failed = 0
    messages = []
    seen_in_file_time = set()
    seen_in_file_fp = set()
    th = get_thresholds(db)
    used_file_date = "measure_time" in resolved
    if not used_file_date:
        messages.append("提醒：檔案未找到「檢測日期／時間」欄，將使用匯入當下時間。請確認 Excel 有日期欄。")

    for idx, row in enumerate(rows):
        try:
            def get(key, default=None):
                col = resolved.get(key)
                if not col:
                    return default
                val = row.get(col)
                if val is None:
                    return default
                # 保留 datetime／數字給 normalize_measure_time 解析
                if key == "measure_time":
                    if isinstance(val, (datetime, )):
                        return val
                    from datetime import date as _date
                    if isinstance(val, _date):
                        return val
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        return val
                    s = str(val).strip()
                    return s if s else default
                if str(val).strip() == "":
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
            muscle_mass = to_float(get("muscle_mass"))
            if smi is None and muscle_mass and height and height > 0:
                # 骨骼肌指數 SMI = 肌肉質量(kg) / 身高(m)^2
                hm = height / 100.0
                smi = round(muscle_mass / (hm * hm), 2)
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

            raw_time = get("measure_time")
            # 「檢測時間」若是 5/10/15 這種秒數，改抓「日期」欄
            def _looks_like_seconds(v):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return 0 <= float(v) < 2000
                s = str(v or "").strip()
                try:
                    return s.isdigit() and int(s) < 2000
                except Exception:
                    return False
            if raw_time is None or _looks_like_seconds(raw_time):
                for alt in ("日期", "檢測日期", "量測日期", "date", "datetime"):
                    if alt in row and row.get(alt) not in (None, ""):
                        raw_time = row.get(alt)
                        break
            measure_date, measure_time = normalize_measure_time(raw_time)
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
    query = db.query(Measurement).filter(_not_deleted())
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
    query = db.query(Measurement).filter(_not_deleted())
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
    vitals = {
        "gender": latest.gender,
        "age": latest.age,
        "grip_strength": latest.grip_strength,
        "chair_stand_time": latest.chair_stand_time,
        "walking_time": latest.walking_time,
        "smi": latest.smi,
        "bp_status": bp_status(latest.systolic, latest.diastolic),
        "sarcopenia_stage": latest.sarcopenia_stage,
    }
    intervention = build_intervention_for_case(db, id_card, vitals, stage=latest.sarcopenia_stage)
    custom_items = get_case_equipment_items(db, id_card)
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
        "intervention_plan": intervention,
        "equipment_plan": {
            "is_custom": custom_items is not None,
            "items": custom_items if custom_items is not None else intervention.get("equipment") or [],
            "source": "custom" if custom_items is not None else "auto",
        },
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
@media print{{.no-print{{display:none}}}}
</style></head><body>
<div class="no-print" style="margin-bottom:12px">
  <button onclick="window.print()" style="padding:8px 16px;font-size:14px;cursor:pointer">列印 / 另存 PDF</button>
  <span style="color:#666;font-size:13px;margin-left:8px">若要存成 PDF：在列印視窗選擇「另存為 PDF」或「Microsoft Print to PDF」</span>
</div>
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


def _ensure_cjk_font() -> Optional[str]:
    """尋找或下載可顯示中文的字型，供 PDF 使用。"""
    candidates = [
        os.path.join(os.path.dirname(__file__), "fonts", "NotoSansTC-Regular.otf"),
        "/tmp/NotoSansTC-Regular.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "C:/Windows/Fonts/msjh.ttc",
        "C:/Windows/Fonts/mingliu.ttc",
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    # 嘗試下載輕量中文字型到 /tmp（僅第一次）
    dest = "/tmp/NotoSansTC-Regular.otf"
    urls = [
        "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/TraditionalChinese/NotoSansTC-Regular.otf",
        "https://cdn.jsdelivr.net/gh/googlefonts/noto-cjk@main/Sans/OTF/TraditionalChinese/NotoSansTC-Regular.otf",
    ]
    try:
        import urllib.request
        for url in urls:
            try:
                urllib.request.urlretrieve(url, dest)
                if os.path.isfile(dest) and os.path.getsize(dest) > 100000:
                    return dest
            except Exception:
                continue
    except Exception:
        pass
    return None


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

    font_path = _ensure_cjk_font()
    has_cjk = False
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    if font_path:
        try:
            pdf.add_font("Noto", "", font_path)
            pdf.set_font("Noto", size=14)
            has_cjk = True
        except Exception:
            has_cjk = False
    if not has_cjk:
        pdf.set_font("Helvetica", size=14)

    def text(s, size=11):
        s = "-" if s is None else str(s)
        if has_cjk:
            try:
                pdf.set_font("Noto", size=size)
                pdf.multi_cell(0, 7, s)
                return
            except Exception:
                pass
        # 無中文字型時：保留數字與英文，中文改為簡易替代，避免整份失敗
        pdf.set_font("Helvetica", size=size)
        safe = s.encode("latin-1", errors="replace").decode("latin-1")
        pdf.multi_cell(0, 7, safe)

    text("寶貝機 · 長者體適能檢測報告", 16)
    text(f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}  操作者：{current_user.display_name}", 9)
    pdf.ln(2)
    text("【基本資料】", 12)
    text(
        f"{latest.user_name} ({latest.id_card})  {g}  {latest.age or '-'} 歲\n"
        f"身高 {latest.height or '-'} cm  體重 {latest.weight or '-'} kg  BMI {latest.bmi or '-'}\n"
        f"最近檢測：{latest.measure_time or '-'}  分期：{latest.sarcopenia_stage or '-'}"
    )
    pdf.ln(1)
    text("【最近指標】", 12)
    text(
        f"握力：{latest.grip_strength if latest.grip_strength is not None else '-'} kg\n"
        f"五次坐站：{latest.chair_stand_time if latest.chair_stand_time is not None else '-'} 秒\n"
        f"走路時間：{latest.walking_time if latest.walking_time is not None else '-'} 秒\n"
        f"SMI：{latest.smi if latest.smi is not None else '-'}\n"
        f"血壓：{latest.systolic or '-'}/{latest.diastolic or '-'} mmHg  脈搏：{latest.pulse or '-'} bpm\n"
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


def _clean_equipment_items(items) -> list:
    cleaned = []
    if not isinstance(items, list):
        return cleaned
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        cleaned.append({
            "id": str(it.get("id") or f"eq_{i+1}"),
            "name": name[:100],
            "why": str(it.get("why") or "")[:500],
            "how": str(it.get("how") or "")[:800],
            "caution": str(it.get("caution") or "")[:400],
            "triggers": [str(t) for t in (it.get("triggers") or ["always_abnormal"])][:10]
            if isinstance(it.get("triggers"), list) else ["always_abnormal"],
        })
    return cleaned


@app.get("/api/cases/{id_card}/equipment", tags=["Cases"])
def get_case_equipment(
    id_card: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取得此個案的運動輔具建議（客製或系統自動）。"""
    custom = get_case_equipment_items(db, id_card)
    if custom is not None:
        return {"id_card": id_card, "is_custom": True, "source": "custom", "items": custom}
    # 用最新量測產生自動建議
    latest = (
        db.query(Measurement)
        .filter(Measurement.id_card == id_card)
        .order_by(Measurement.measure_time.desc())
        .first()
    )
    if not latest:
        return {"id_card": id_card, "is_custom": False, "source": "auto", "items": get_equipment_catalog(db)}
    vitals = {
        "gender": latest.gender,
        "grip_strength": latest.grip_strength,
        "chair_stand_time": latest.chair_stand_time,
        "walking_time": latest.walking_time,
        "smi": latest.smi,
        "bp_status": bp_status(latest.systolic, latest.diastolic),
        "sarcopenia_stage": latest.sarcopenia_stage,
    }
    plan = build_intervention_for_case(db, id_card, vitals, stage=latest.sarcopenia_stage)
    return {
        "id_card": id_card,
        "is_custom": False,
        "source": "auto",
        "items": plan.get("equipment") or [],
        "user_name": latest.user_name,
    }


@app.put("/api/cases/{id_card}/equipment", tags=["Cases"])
def put_case_equipment(
    id_card: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    """為單一個案儲存客製化運動輔具建議（覆蓋系統自動）。"""
    if CaseEquipmentPlan is None:
        raise HTTPException(500, "CaseEquipmentPlan 模型未載入")
    items = _clean_equipment_items(payload.get("items"))
    if not items:
        raise HTTPException(400, "請至少保留一筆輔具建議")
    note = (payload.get("note") or "").strip() or None
    # 取姓名
    latest = (
        db.query(Measurement)
        .filter(Measurement.id_card == id_card)
        .order_by(Measurement.measure_time.desc())
        .first()
    )
    user_name = (latest.user_name if latest else None) or payload.get("user_name")
    row = db.query(CaseEquipmentPlan).filter(CaseEquipmentPlan.id_card == id_card).first()
    value = json.dumps(items, ensure_ascii=False)
    if row:
        row.items_json = value
        row.note = note
        row.user_name = user_name
        row.updated_by = current_user.username
    else:
        db.add(CaseEquipmentPlan(
            id_card=id_card,
            user_name=user_name,
            items_json=value,
            note=note,
            updated_by=current_user.username,
        ))
    db.commit()
    add_audit(db, current_user.username, "case_equipment_save", f"{id_card} count={len(items)}")
    return {"ok": True, "id_card": id_card, "is_custom": True, "source": "custom", "items": items}


@app.delete("/api/cases/{id_card}/equipment", tags=["Cases"])
def delete_case_equipment(
    id_card: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    """刪除個案客製建議，恢復系統依數據自動產生。"""
    if CaseEquipmentPlan is None:
        raise HTTPException(500, "CaseEquipmentPlan 模型未載入")
    row = db.query(CaseEquipmentPlan).filter(CaseEquipmentPlan.id_card == id_card).first()
    if row:
        db.delete(row)
        db.commit()
        add_audit(db, current_user.username, "case_equipment_reset", id_card)
    return {"ok": True, "id_card": id_card, "is_custom": False, "source": "auto"}


@app.get("/api/settings/equipment", tags=["Settings"])
def get_equipment_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取得預設範本運動輔具建議（僅在「沒有個案客製」時使用）。"""
    return {"items": get_equipment_catalog(db), "note": "此為預設範本；實際通報以個案客製為優先。"}


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
    try:
        from standards import get_standards
        std = get_standards(age, gender)
    except Exception:
        std = None
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
    if Feedback is None:
        raise HTTPException(500, "Feedback 資料表尚未部署，請上傳最新 models.py")
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
    if Feedback is None:
        raise HTTPException(500, "Feedback 資料表尚未部署，請上傳最新 models.py")
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


def _issue_fingerprint(issue_type: str, related_id: str = "", title: str = "") -> str:
    return f"{issue_type}|{related_id}|{title}"[:120]


def _upsert_open_issue(
    db: Session,
    issue_type: str,
    title: str,
    detail: str = "",
    related_id: str = "",
    severity: str = "warning",
    auto_fixed: bool = False,
    status: str = "open",
):
    if SystemIssue is None:
        return None
    fp = _issue_fingerprint(issue_type, related_id, title)
    existing = (
        db.query(SystemIssue)
        .filter(SystemIssue.fingerprint == fp, SystemIssue.status == "open")
        .first()
    )
    if existing:
        existing.detail = detail
        existing.severity = severity
        existing.auto_fixed = auto_fixed
        if status != "open":
            existing.status = status
            existing.resolved_at = datetime.now(timezone.utc)
            existing.resolved_by = "system"
        return existing
    row = SystemIssue(
        issue_type=issue_type,
        severity=severity,
        title=title,
        detail=detail,
        related_id=related_id or None,
        fingerprint=fp,
        auto_fixed=auto_fixed,
        status=status,
        resolved_by="system" if status != "open" else None,
        resolved_at=datetime.now(timezone.utc) if status != "open" else None,
    )
    db.add(row)
    return row


def run_self_heal(db: Session, trigger: str = "manual") -> dict:
    """
    每日／手動系統健檢：
    - 可自動排除：重複資料、缺 BMI、分期不一致、性別正規化
    - 無法排除：缺身分證／姓名、孤懸通報等 → 開立待辦給管理員
    """
    summary = {
        "trigger": trigger,
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "auto_fixed": [],
        "opened": [],
        "counts": {},
    }
    if SystemIssue is None:
        summary["error"] = "SystemIssue 模型未部署"
        return summary

    # 1) 重複檢測資料
    try:
        deleted, names = purge_duplicate_measurements(db)
        if deleted:
            msg = f"已自動刪除 {deleted} 筆重複檢測；例：{', '.join(names[:5])}"
            summary["auto_fixed"].append({"type": "duplicate", "detail": msg})
            _upsert_open_issue(
                db, "duplicate", f"已自動排除 {deleted} 筆重複資料", msg,
                related_id="batch", severity="info", auto_fixed=True, status="fixed",
            )
        summary["counts"]["duplicates_deleted"] = deleted
    except Exception as e:
        summary["opened"].append({"type": "duplicate_error", "detail": str(e)})
        _upsert_open_issue(db, "heal_error", "重複資料排除失敗", str(e), severity="error")

    # 2) 補算 BMI、重算分期／異常數
    fixed_bmi = 0
    fixed_stage = 0
    try:
        rows = db.query(Measurement).order_by(Measurement.id.asc()).limit(5000).all()
        for rec in rows:
            changed = False
            # BMI
            if (rec.bmi is None or rec.bmi == 0) and rec.height and rec.weight:
                try:
                    new_bmi = calc_bmi(rec.height, rec.weight)
                    if new_bmi:
                        rec.bmi = new_bmi
                        fixed_bmi += 1
                        changed = True
                except Exception:
                    pass
            # 性別正規化
            g = (rec.gender or "").strip().upper()
            if g in ("男", "MALE"):
                rec.gender = "M"
                changed = True
            elif g in ("女", "FEMALE"):
                rec.gender = "F"
                changed = True
            elif g not in ("M", "F") and g:
                _upsert_open_issue(
                    db, "invalid_gender",
                    f"性別無法辨識：{rec.user_name or ''}（{rec.id_card}）",
                    f"目前值={rec.gender}，紀錄 id={rec.id}",
                    related_id=str(rec.id),
                    severity="warning",
                )
            # 重算分期
            try:
                stage, abn, status_text = judge_sarcopenia(
                    rec.gender, rec.grip_strength, rec.chair_stand_time,
                    rec.walking_time, rec.smi, age=rec.age,
                )
                if stage != (rec.sarcopenia_stage or "") or abn != (rec.abnormal_count or 0):
                    rec.sarcopenia_stage = stage
                    rec.abnormal_count = abn
                    rec.status = status_text
                    fixed_stage += 1
                    changed = True
            except Exception:
                pass
            # 缺關鍵欄位
            if not (rec.id_card or "").strip():
                _upsert_open_issue(
                    db, "missing_id_card",
                    f"檢測紀錄缺身分證（id={rec.id}）",
                    f"姓名={rec.user_name} 時間={rec.measure_time}",
                    related_id=str(rec.id),
                    severity="error",
                )
            if not (rec.user_name or "").strip():
                _upsert_open_issue(
                    db, "missing_name",
                    f"檢測紀錄缺姓名（{rec.id_card or rec.id}）",
                    f"紀錄 id={rec.id}",
                    related_id=str(rec.id),
                    severity="warning",
                )
        if fixed_bmi or fixed_stage:
            db.commit()
        if fixed_bmi:
            summary["auto_fixed"].append({"type": "bmi", "detail": f"補算 BMI {fixed_bmi} 筆"})
            _upsert_open_issue(
                db, "bmi_fix", f"已自動補算 BMI {fixed_bmi} 筆", "",
                auto_fixed=True, status="fixed", severity="info",
            )
        if fixed_stage:
            summary["auto_fixed"].append({"type": "stage", "detail": f"重算分期／異常 {fixed_stage} 筆"})
            _upsert_open_issue(
                db, "stage_fix", f"已自動重算分期 {fixed_stage} 筆", "",
                auto_fixed=True, status="fixed", severity="info",
            )
        summary["counts"]["bmi_fixed"] = fixed_bmi
        summary["counts"]["stage_fixed"] = fixed_stage
    except Exception as e:
        summary["opened"].append({"type": "recalc_error", "detail": str(e)})
        _upsert_open_issue(db, "heal_error", "BMI／分期重算失敗", str(e), severity="error")

    # 3) 孤懸通報（measurement_id 指向不存在的紀錄）
    try:
        orphan = 0
        alerts = db.query(Alert).filter(Alert.measurement_id.isnot(None)).limit(2000).all()
        for a in alerts:
            exists = db.query(Measurement.id).filter(Measurement.id == a.measurement_id).first()
            if not exists:
                orphan += 1
                _upsert_open_issue(
                    db, "orphan_alert",
                    f"孤懸異常通報：{a.user_name}（{a.id_card}）",
                    f"alert_id={a.id} measurement_id={a.measurement_id} 已不存在，請確認是否標記處理或刪除",
                    related_id=str(a.id),
                    severity="warning",
                )
        summary["counts"]["orphan_alerts"] = orphan
    except Exception as e:
        _upsert_open_issue(db, "heal_error", "孤懸通報檢查失敗", str(e), severity="error")

    # 記錄本次執行時間
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == "last_self_heal").first()
        val = json.dumps(summary, ensure_ascii=False)[:4000]
        if row:
            row.value = val
            row.updated_by = "system"
        else:
            db.add(SystemConfig(key="last_self_heal", value=val, updated_by="system"))
        db.commit()
    except Exception:
        try:
            db.commit()
        except Exception:
            db.rollback()

    open_count = 0
    if SystemIssue is not None:
        open_count = db.query(SystemIssue).filter(SystemIssue.status == "open").count()
    summary["counts"]["open_issues"] = open_count
    summary["counts"]["auto_fixed_n"] = len(summary["auto_fixed"])
    return summary


def maybe_daily_self_heal(db: Session) -> Optional[dict]:
    """若距上次執行超過 24 小時則自動跑一次。"""
    try:
        row = db.query(SystemConfig).filter(SystemConfig.key == "last_self_heal").first()
        if row and row.value:
            try:
                prev = json.loads(row.value)
                ran = prev.get("ran_at") or ""
                if ran:
                    last = datetime.strptime(ran[:19], "%Y-%m-%d %H:%M:%S")
                    if (datetime.now() - last).total_seconds() < 24 * 3600:
                        return None
            except Exception:
                pass
        return run_self_heal(db, trigger="daily_auto")
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/admin/self-heal", tags=["System"])
def api_run_self_heal(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    """手動執行系統健檢與自動排除。"""
    result = run_self_heal(db, trigger=f"manual:{current_user.username}")
    add_audit(db, current_user.username, "self_heal", json.dumps(result.get("counts", {}), ensure_ascii=False))
    return result


@app.get("/api/admin/self-heal/status", tags=["System"])
def api_self_heal_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    """查詢上次健檢結果，並在超過 24 小時時自動再跑一次。"""
    auto = maybe_daily_self_heal(db)
    row = db.query(SystemConfig).filter(SystemConfig.key == "last_self_heal").first()
    last = None
    if row and row.value:
        try:
            last = json.loads(row.value)
        except Exception:
            last = {"raw": row.value}
    open_count = 0
    if SystemIssue is not None:
        open_count = db.query(SystemIssue).filter(SystemIssue.status == "open").count()
    return {
        "last": last,
        "auto_ran_now": auto is not None,
        "auto_result": auto,
        "open_issues": open_count,
    }


@app.get("/api/admin/issues", tags=["System"])
def list_system_issues(
    status: str = Query("open"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    if SystemIssue is None:
        raise HTTPException(500, "SystemIssue 尚未部署，請上傳最新 models.py")
    # 進入此頁時順便觸發每日健檢
    maybe_daily_self_heal(db)
    q = db.query(SystemIssue)
    if status and status != "all":
        q = q.filter(SystemIssue.status == status)
    total = q.count()
    rows = (
        q.order_by(SystemIssue.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "items": [
            {
                "id": r.id,
                "issue_type": r.issue_type,
                "severity": r.severity,
                "title": r.title,
                "detail": r.detail,
                "related_id": r.related_id,
                "auto_fixed": r.auto_fixed,
                "status": r.status,
                "resolved_by": r.resolved_by,
                "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
                "resolve_note": r.resolve_note,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@app.post("/api/admin/issues/{issue_id}/resolve", tags=["System"])
def resolve_system_issue(
    issue_id: int,
    payload: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles("admin", "superadmin")),
):
    if SystemIssue is None:
        raise HTTPException(500, "SystemIssue 尚未部署")
    row = db.query(SystemIssue).get(issue_id)
    if not row:
        raise HTTPException(404, "找不到此問題")
    action = (payload.get("action") or "fixed").lower()
    if action not in ("fixed", "ignored"):
        action = "fixed"
    row.status = action if action == "ignored" else "fixed"
    if action == "ignored":
        row.status = "ignored"
    row.resolved_by = current_user.username
    row.resolved_at = datetime.now(timezone.utc)
    row.resolve_note = (payload.get("note") or "").strip() or None
    db.commit()
    add_audit(db, current_user.username, "resolve_issue", f"id={issue_id} status={row.status}")
    return {"ok": True, "id": issue_id, "status": row.status}


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
