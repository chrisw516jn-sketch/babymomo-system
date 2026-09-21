from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import List, Optional
from datetime import timedelta
import io
import csv
from openpyxl import load_workbook

from database import engine, get_db, Base
from models import User, Measurement, AuditLog, Alert
from schemas import (
    UserCreate, UserOut, Token, MeasurementCreate, MeasurementOut,
    StatsOut, ImportResult
)
from auth import (
    get_password_hash, verify_password, create_access_token,
    get_current_user, require_roles, get_user_by_username, ACCESS_TOKEN_EXPIRE_MINUTES
)
from utils import calc_bmi, judge_sarcopenia, normalize_measure_time, format_alert_message, bp_status
from line_notify import send_line_text, line_configured

# ---------- App ----------
app = FastAPI(
    title="寶貝機 長者體適能與肌少衰弱檢測分析系統 API",
    description="真實可用的後端 API，支援 CSV/Excel 匯入、多人登入、RESTful 串接",
    version="1.0.0",
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

# ---------- Startup ----------
# ---------- Startup ----------
@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        for old in db.query(User).all():
            if old.username not in {"bonnie", "chrisavicii", "littlethanks", "Netown", "nurse1", "care1"}:
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
        allowed = {"bonnie", "chrisavicii", "littlethanks", "Netown", "nurse1", "care1"}
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
    finally:
        db.close()




def add_audit(db: Session, operator: str, action: str, details: str = ""):
    db.add(AuditLog(operator=operator, action=action, details=details))
    db.commit()


def create_abnormal_alert(db: Session, rec: Measurement):
    """身體數據異常時，建立通報給護理師與照顧服務員。"""
    if not rec:
        return None
    # 無異常不通報
    stage = rec.sarcopenia_stage or "正常"
    abn = rec.abnormal_count or 0
    if abn <= 0 and stage == "正常":
        return None

    if stage == "嚴重肌少症" or abn >= 3:
        severity = "critical"
        title = f"【緊急】{rec.user_name} 身體數據多重異常"
    elif stage in ("肌少症", "肌少症前期") or abn >= 1:
        severity = "warning"
        title = f"【注意】{rec.user_name} 檢測異常需關懷"
    else:
        severity = "info"
        title = f"【提醒】{rec.user_name} 指標需追蹤"

    message = format_alert_message(rec)

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
    # 公開註冊已關閉，僅允許系統預設的兩個管理員帳號
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


# ========== Measurements ==========
@app.post("/api/measurements", response_model=MeasurementOut, tags=["Measurements"])
def create_measurement(
    payload: MeasurementCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    measure_date, measure_time = normalize_measure_time(payload.measure_time)

    # 去重
    exists = db.query(Measurement).filter(
        Measurement.id_card == payload.id_card,
        Measurement.measure_time == measure_time,
    ).first()
    if exists:
        raise HTTPException(409, "同一身分證於同一時間點已有紀錄，已略過")

    bmi = calc_bmi(payload.height, payload.weight) or payload.bmi
    stage, abn_count, status_text = judge_sarcopenia(
        payload.gender, payload.grip_strength, payload.chair_stand_time,
        payload.walking_time, payload.smi
    )

    # 血壓額外提示
    if payload.systolic and payload.systolic >= 140:
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
    rows = db.query(Measurement.id, Measurement.id_card, Measurement.measure_date, Measurement.measure_time).all()
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
    return {"total": total, "page": page, "page_size": page_size, "items": [MeasurementOut.model_validate(i) for i in items]}


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


# ========== Import CSV / Excel ==========
@app.post("/api/import", response_model=ImportResult, tags=["Import"])
async def import_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    filename = (file.filename or "").lower()
    content = await file.read()

    # 讀取成 list of dicts（不依賴 pandas）
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

    # 欄位對應（支援中英文欄位名）
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

    # 建立實際欄位對應
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
                except:
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

            # 生理防呆
            if grip is not None and (grip < 0 or grip > 80):
                failed += 1
                messages.append(f"第 {idx+2} 列：握力數值異常 ({grip})")
                continue
            if systolic is not None and (systolic < 50 or systolic > 250):
                failed += 1
                messages.append(f"第 {idx+2} 列：收縮壓異常 ({systolic})")
                continue

            measure_date, measure_time = normalize_measure_time(get("measure_time"))

            # 去重
            exists = db.query(Measurement).filter(
                Measurement.id_card == id_card,
                Measurement.measure_time == measure_time,
            ).first()
            if exists:
                skipped += 1
                continue

            bmi = calc_bmi(height, weight)
            stage, abn_count, status_text = judge_sarcopenia(gender, grip, chair, walk, smi)

            if systolic and systolic >= 140:
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
    add_audit(
        db, current_user.username, "import",
        f"file={filename} success={success} skipped={skipped} failed={failed}"
    )
    return ImportResult(success=success, skipped=skipped, failed=failed, messages=messages[:30])


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
            total_records=0,
            unique_users=0,
            avg_grip=0,
            avg_chair=0,
            avg_walk=0,
            multi_abnormal_rate=0,
            summary_text="所選區間無檢測數據。",
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
        "avg_grip": [
            round(sum(month_map[m]["grips"]) / len(month_map[m]["grips"]), 1)
            if month_map[m]["grips"] else 0
            for m in sorted_months
        ],
        "avg_chair": [
            round(sum(month_map[m]["chairs"]) / len(month_map[m]["chairs"]), 1)
            if month_map[m]["chairs"] else 0
            for m in sorted_months
        ],
        "avg_walk": [
            round(sum(month_map[m]["walks"]) / len(month_map[m]["walks"]), 1)
            if month_map[m]["walks"] else 0
            for m in sorted_months
        ],
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
    # BOM for Excel
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


# ========== Case detail (for trend) ==========
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
    }



# ========== Alerts 異常通報 ==========
@app.get("/api/alerts", tags=["Alerts"])
def list_alerts(
    only_unread: bool = False,
    only_unhandled: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Alert)
    # 依角色過濾：護理師/照顧服務員只看目標含自己角色的通報
    role = current_user.role or "staff"
    if role not in ("superadmin", "admin", "company_admin"):
        # SQLite 用 like 過濾 target_roles
        q = q.filter(Alert.target_roles.ilike(f"%{role}%"))
    if only_unread:
        q = q.filter(Alert.is_read == False)
    if only_unhandled:
        q = q.filter(Alert.is_handled == False)
    total = q.count()
    items = (
        q.order_by(Alert.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "unread": db.query(Alert).filter(Alert.is_read == False).count(),
        "unhandled": db.query(Alert).filter(Alert.is_handled == False).count(),
        "page": page,
        "items": [
            {
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
                "is_handled": a.is_handled,
                "handled_by": a.handled_by,
                "handle_note": a.handle_note,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "vitals": _vitals_for_alert(db, a),
            }
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
    from datetime import datetime, timezone
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
    from datetime import datetime, timezone
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
    add_audit(db, current_user.username, "handle_alert", f"alert={alert_id} {a.user_name}")
    return {"ok": True}


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
    """先送一筆模擬異常資料。未設定 LINE Token 時只回傳預覽，不會真的發到個人 LINE ID。"""
    sample = (
        "【寶貝機模擬通報】\n"
        "個案：王小明（A123456789）\n"
        "性別/年齡：男 / 72 歲\n"
        "檢測時間：2026-09-14 10:00:00\n"
        "身高/體重：168 cm / 65 kg\n"
        "BMI：23.0\n"
        "握力：16.2 kg（不足）\n"
        "五次坐站：14.8 秒（偏慢）\n"
        "走路時間：22.1 秒（偏慢）\n"
        "SMI：6.4（偏低）\n"
        "血壓：168/98 mmHg（血壓偏高）\n"
        "脈搏：88 bpm\n"
        "肌少症分期：嚴重肌少症\n"
        "異常項目數：4\n"
        "說明：這是測試訊息，用來確認 LINE 通報格式。\n"
        f"操作者：{current_user.username}"
    )
    result = send_line_text(sample)
    result["configured"] = line_configured()
    result["note"] = (
        "LINE 無法用一般 ID（例如 chrischuang1118）直接傳訊。"
        "請建立 LINE 官方帳號 Messaging API，把 Channel Access Token 與你的 userId 設到 Railway 變數後才會真的送到 LINE。"
    )
    return result


# ========== Health check ==========
@app.get("/api/health", tags=["System"])
def health():
    return {"status": "ok", "service": "寶貝機體適能檢測系統"}


# 掛載前端靜態檔（若存在）
import os
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
