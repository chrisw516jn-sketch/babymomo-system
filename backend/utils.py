"""業務邏輯：肌少症分期判斷、BMI 計算、異常項目統計"""

from typing import Optional, Tuple


def calc_bmi(height_cm: Optional[float], weight_kg: Optional[float]) -> Optional[float]:
    if not height_cm or not weight_kg or height_cm <= 0:
        return None
    h = height_cm / 100.0
    return round(weight_kg / (h * h), 1)


def judge_sarcopenia(
    gender: str,
    grip: Optional[float],
    chair: Optional[float],
    walk: Optional[float],
    smi: Optional[float],
) -> Tuple[str, int, str]:
    """
    依衛福部 / AWGS 2019 簡易標準判斷肌少症分期。
    回傳 (stage, abnormal_count, status_text)
    """
    is_male = gender.upper() in ("M", "男")
    grip_std = 28.0 if is_male else 18.0
    smi_std = 7.0 if is_male else 5.7

    issues = []
    low_muscle = False   # 肌力或肌肉量不足
    low_function = False # 身體功能不足

    if grip is not None:
        if grip < grip_std:
            issues.append(f"握力不足 ({grip:.1f} kg)")
            low_muscle = True

    if smi is not None:
        if smi < smi_std:
            issues.append(f"SMI肌肉量偏低 ({smi:.1f})")
            low_muscle = True

    if chair is not None:
        if chair >= 12.0:
            issues.append(f"坐站偏慢 ({chair:.2f} s)")
            low_function = True

    if walk is not None:
        if walk >= 20.0:  # 約等於 6 公尺 < 0.8 m/s 的簡化
            issues.append(f"步速偏慢 ({walk:.2f} s)")
            low_function = True

    # 分期邏輯（簡化版）
    if low_muscle and low_function:
        stage = "嚴重肌少症"
    elif low_muscle:
        stage = "肌少症"
    elif low_function:
        stage = "肌少症前期"
    else:
        stage = "正常"

    # 血壓額外提示（不影響分期）
    # 這裡只在 status 顯示

    status = " / ".join(issues) if issues else "各項指標正常"
    return stage, len(issues), status


def bp_status(systolic, diastolic) -> str:
    if systolic is None or diastolic is None:
        return "未量測"
    if systolic >= 180 or diastolic >= 120:
        return "高血壓危急"
    if systolic >= 160 or diastolic >= 100:
        return "血壓偏高（需關懷）"
    if systolic >= 140 or diastolic >= 90:
        return "血壓偏高"
    if systolic < 90 or diastolic < 60:
        return "血壓偏低"
    return "血壓正常"


def format_alert_message(rec) -> str:
    """組成通報與 LINE 訊息內容（含異常標記）。"""
    stage = getattr(rec, "sarcopenia_stage", None) or "正常"
    abn = getattr(rec, "abnormal_count", 0) or 0
    bp = bp_status(getattr(rec, "systolic", None), getattr(rec, "diastolic", None))
    gender = getattr(rec, "gender", None) or ""
    g = "男" if str(gender).upper() in ("M", "男") else ("女" if str(gender).upper() in ("F", "女") else gender)

    grip = getattr(rec, "grip_strength", None)
    chair = getattr(rec, "chair_stand_time", None)
    walk = getattr(rec, "walking_time", None)
    smi = getattr(rec, "smi", None)

    def mark(ok: bool, text: str) -> str:
        return (("⚠️ " if not ok else "✓ ") + text)

    grip_ok = True
    if grip is not None:
        if str(gender).upper() in ("M", "男"):
            grip_ok = grip >= 28
        elif str(gender).upper() in ("F", "女"):
            grip_ok = grip >= 18

    chair_ok = True if chair is None else chair <= 12
    walk_ok = True if walk is None else walk <= 18
    smi_ok = True
    if smi is not None:
        if str(gender).upper() in ("M", "男"):
            smi_ok = smi >= 7.0
        elif str(gender).upper() in ("F", "女"):
            smi_ok = smi >= 5.7
    bp_ok = bp in ("血壓正常", "未量測")

    return (
        f"【寶貝機異常通報】\n"
        f"個案：{rec.user_name}（{rec.id_card}）\n"
        f"性別/年齡：{g} / {rec.age or '-'} 歲\n"
        f"檢測時間：{rec.measure_time or rec.measure_date or '-'}\n"
        f"────────────────\n"
        f"{mark(grip_ok, f'握力：{grip if grip is not None else "-"} kg')}\n"
        f"{mark(chair_ok, f'五次坐站：{chair if chair is not None else "-"} 秒')}\n"
        f"{mark(walk_ok, f'走路時間：{walk if walk is not None else "-"} 秒')}\n"
        f"{mark(smi_ok, f'SMI：{smi if smi is not None else "-"}')}\n"
        f"{mark(bp_ok, f'血壓：{rec.systolic or "-"}/{rec.diastolic or "-"} mmHg（{bp}）')}\n"
        f"脈搏：{rec.pulse or '-'} bpm\n"
        f"BMI：{rec.bmi if rec.bmi is not None else '-'}　身高/體重：{rec.height or '-'} cm / {rec.weight or '-'} kg\n"
        f"────────────────\n"
        f"肌少症分期：{stage}\n"
        f"異常項目數：{abn}\n"
        f"說明：{rec.status or '-'}\n"
        f"請護理師／照顧服務員盡快關懷並記錄處理。"
    )


def normalize_measure_time(raw: Optional[str]) -> Tuple[str, str]:
    """回傳 (measure_date, measure_time)"""
    from datetime import datetime
    if not raw:
        now = datetime.now()
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")

    raw = str(raw).strip().replace("/", "-")
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    # fallback
    now = datetime.now()
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M:%S")
