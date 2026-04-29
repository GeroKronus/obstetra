"""Interface web admin para a secretária cadastrar/editar pacientes.

Rotas:
- GET  /admin/                — lista de pacientes
- GET  /admin/novo            — formulário em branco
- POST /admin/novo            — cria paciente
- GET  /admin/{phone}         — ficha pre-preenchida
- POST /admin/{phone}         — atualiza paciente
- GET  /admin/agenda          — visão dia/semana de consultas
- GET  /admin/agenda/nova     — form nova consulta
- POST /admin/agenda/nova     — cria consulta
- GET  /admin/agenda/{id}/editar — edita consulta
- POST /admin/agenda/{id}     — atualiza consulta
- POST /admin/agenda/{id}/cancelar — cancela
- GET  /admin/lembretes       — lista lembretes do dia

Autenticação: HTTP Basic. Credenciais lidas do Tenant default no DB
(seedado a partir de ADMIN_USER/ADMIN_PASSWORD no env).

Storage: tudo no DB (Postgres/SQLite). Sem vault, sem markdown.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from .config import settings
from .db import engine
from .models import (
    Appointment,
    AppointmentStatus,
    AppointmentType,
    OnboardingState,
    Patient,
    PatientStatus,
    ScheduledMessage,
    Tenant,
    utcnow,
)


log = logging.getLogger("obstetra.admin")
security = HTTPBasic()

_BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

router = APIRouter(prefix="/admin")

DEFAULT_TENANT_ID = 1


# =====================================================================
# Auth — resolve credenciais do Tenant default no DB
# =====================================================================

def verify_admin(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> int:
    """Valida HTTP Basic contra Tenant.admin_user/admin_password_hash no DB.
    Retorna o tenant_id correspondente. Fallback pra env se DB ausente."""
    with Session(engine) as db:
        tenant = db.exec(select(Tenant).where(Tenant.id == DEFAULT_TENANT_ID)).first()

    if tenant:
        user_match = secrets.compare_digest(
            credentials.username.encode("utf-8"),
            tenant.admin_user.encode("utf-8"),
        )
        try:
            pass_match = bcrypt.checkpw(
                credentials.password.encode("utf-8"),
                tenant.admin_password_hash.encode("utf-8"),
            )
        except Exception:
            pass_match = False
        if user_match and pass_match:
            return tenant.id or DEFAULT_TENANT_ID

    # Fallback pra env (caso bootstrap nao tenha rodado)
    if settings.admin_user and settings.admin_password:
        u = secrets.compare_digest(credentials.username.encode("utf-8"), settings.admin_user.encode("utf-8"))
        p = secrets.compare_digest(credentials.password.encode("utf-8"), settings.admin_password.encode("utf-8"))
        if u and p:
            return DEFAULT_TENANT_ID

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciais inválidas",
        headers={"WWW-Authenticate": "Basic"},
    )


# =====================================================================
# Helpers
# =====================================================================

def _clean_phone(raw: str) -> str | None:
    """Normaliza telefone pro formato E.164 sem `+` (ex: 5528988030050).
    Aceita DDD + número (10-11 dígitos) prependendo '55'.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if digits.startswith("55") and 12 <= len(digits) <= 13:
        return digits
    if 10 <= len(digits) <= 11:
        return "55" + digits
    return None


def _date_str(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _patient_to_dict(p: Patient) -> dict:
    """Converte Patient row em dict pronto pra template (mesmas chaves que antes)."""
    return {
        "nome": p.name or "",
        "telefone": p.phone,
        "data_nascimento": _date_str(p.data_nascimento),
        "endereco": p.endereco or "",
        "dum": _date_str(p.dum),
        "tipo_gestacao": p.tipo_gestacao or "unica",
        "risco": p.risco or "habitual",
        "gestacao_planejada": bool(p.gestacao_planejada),
        "gestas": p.gestas if p.gestas is not None else 0,
        "partos_normais": p.partos_normais if p.partos_normais is not None else 0,
        "cesareas": p.cesareas if p.cesareas is not None else 0,
        "abortos": p.abortos if p.abortos is not None else 0,
        "alergias": p.alergias or "",
        "condicoes_pre_existentes": p.condicoes_pre_existentes or "",
        "medicacoes_em_uso": p.medicacoes_em_uso or "",
        "grupo_sanguineo": p.grupo_sanguineo or "",
        "medico_obstetra": p.medico_obstetra or settings.doctor_name,
        "hospital_referencia": p.hospital_referencia or "",
        "plano_saude": p.plano_saude or "",
        "contato_emergencia_nome": p.contato_emergencia_nome or "",
        "contato_emergencia_telefone": p.contato_emergencia_telefone or "",
        "contato_emergencia_relacao": p.contato_emergencia_relacao or "",
        "preferencias_atendimento": p.preferencias_atendimento or "",
        "historico_clinico": p.historico_clinico or "",
        "historico_obstetrico": p.historico_obstetrico or "",
        "observacoes_dra": p.observacoes_dra or "",
        "status": p.status.value if hasattr(p.status, "value") else (p.status or "ativa"),
    }


def _build_search_blob(p: Patient) -> str:
    """Concatena todos os campos pesquisaveis num unico string lowercase."""
    parts = [
        p.name, p.phone, _date_str(p.data_nascimento), p.endereco,
        p.tipo_gestacao, p.risco, p.grupo_sanguineo,
        p.alergias, p.condicoes_pre_existentes, p.medicacoes_em_uso,
        p.medico_obstetra, p.hospital_referencia, p.plano_saude,
        p.contato_emergencia_nome, p.contato_emergencia_telefone, p.contato_emergencia_relacao,
        p.preferencias_atendimento,
        p.historico_clinico, p.historico_obstetrico, p.observacoes_dra,
        (p.status.value if hasattr(p.status, "value") else str(p.status or "")),
    ]
    return " ".join(str(x) for x in parts if x).lower()


# =====================================================================
# Listagem
# =====================================================================

@router.get("/", response_class=HTMLResponse)
async def admin_index(
    request: Request,
    q: str = "",
    tenant_id: int = Depends(verify_admin),
):
    query = (q or "").strip().lower()
    patients: list[dict] = []
    total = 0
    today = date.today()

    with Session(engine) as db:
        rows = db.exec(
            select(Patient)
            .where(Patient.tenant_id == tenant_id)
            .where(Patient.name.is_not(None))
            .order_by(Patient.name)
        ).all()
        total = len(rows)

        for p in rows:
            if query:
                blob = _build_search_blob(p)
                if query not in blob:
                    continue

            # Semanas a partir da DUM (preferida) ou fallback
            semanas: int | None = None
            if p.dum:
                delta = (today - p.dum).days
                if delta >= 0:
                    semanas = delta // 7
            elif p.gestational_weeks is not None:
                semanas = p.gestational_weeks

            patients.append({
                "phone": p.phone,
                "nome": p.name,
                "semanas": semanas,
                "tipo": p.tipo_gestacao or "-",
                "risco": p.risco or "-",
                "status": (p.status.value if hasattr(p.status, "value") else (p.status or "ativa")),
            })

    return templates.TemplateResponse(
        request,
        "admin/list.html",
        {"patients": patients, "q": q, "total": total},
    )


@router.get("/novo", response_class=HTMLResponse)
async def admin_novo_form(request: Request, _: int = Depends(verify_admin)):
    return templates.TemplateResponse(
        request,
        "admin/form.html",
        {"patient": None, "phone_lock": False, "error": None},
    )


# =====================================================================
# Agenda
# =====================================================================

def _day_window_utc(target_date: date) -> tuple[datetime, datetime]:
    """(start_utc, end_utc) cobrindo BRT 00:00 a 23:59 do dia."""
    brt = ZoneInfo("America/Sao_Paulo")
    start_brt = datetime.combine(target_date, datetime.min.time(), tzinfo=brt)
    end_brt = datetime.combine(target_date, datetime.max.time(), tzinfo=brt)
    return (
        start_brt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
        end_brt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
    )


def _parse_target_date(data: str) -> date:
    brt = ZoneInfo("America/Sao_Paulo")
    if data:
        try:
            return date.fromisoformat(data.strip())
        except ValueError:
            pass
    return datetime.now(brt).date()


def _monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())


@router.get("/agenda", response_class=HTMLResponse)
async def admin_agenda(
    request: Request,
    data: str = "",
    vista: str = "dia",
    inicio: str = "",
    tenant_id: int = Depends(verify_admin),
):
    """vista=dia (default) ou vista=semana"""
    if vista == "semana":
        return await _admin_agenda_semana(request, inicio, tenant_id)

    brt = ZoneInfo("America/Sao_Paulo")
    target_date = _parse_target_date(data)
    start_utc, end_utc = _day_window_utc(target_date)

    items: list[dict] = []
    with Session(engine) as db:
        rows = db.exec(
            select(Appointment, Patient)
            .join(Patient, Appointment.patient_id == Patient.id)
            .where(Appointment.tenant_id == tenant_id)
            .where(Appointment.scheduled_at >= start_utc)
            .where(Appointment.scheduled_at <= end_utc)
            .order_by(Appointment.scheduled_at)
        ).all()
        for ap, pat in rows:
            when_brt = ap.scheduled_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(brt)
            items.append({
                "id": ap.id,
                "patient_phone": pat.phone,
                "patient_name": pat.name or pat.phone,
                "hora": when_brt.strftime("%H:%M"),
                "duracao_min": ap.duracao_min,
                "tipo": ap.tipo.value if hasattr(ap.tipo, "value") else str(ap.tipo),
                "obs": ap.obs or "",
                "status": ap.status.value if hasattr(ap.status, "value") else str(ap.status),
            })

    today = datetime.now(brt).date()
    prev_day = (target_date - timedelta(days=1)).isoformat()
    next_day = (target_date + timedelta(days=1)).isoformat()

    return templates.TemplateResponse(
        request,
        "admin/agenda.html",
        {
            "items": items,
            "target_date": target_date,
            "target_date_iso": target_date.isoformat(),
            "is_today": target_date == today,
            "prev_day": prev_day,
            "next_day": next_day,
            "today_iso": today.isoformat(),
        },
    )


async def _admin_agenda_semana(request: Request, inicio: str, tenant_id: int) -> HTMLResponse:
    brt = ZoneInfo("America/Sao_Paulo")
    base_date = _parse_target_date(inicio)
    week_start = _monday_of_week(base_date)
    week_end = week_start + timedelta(days=6)

    start_utc, _ = _day_window_utc(week_start)
    _, end_utc = _day_window_utc(week_end)

    days: list[dict] = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        days.append({
            "date": d,
            "date_iso": d.isoformat(),
            "weekday": ['Segunda','Terça','Quarta','Quinta','Sexta','Sábado','Domingo'][i],
            "consultas": [],
        })

    with Session(engine) as db:
        rows = db.exec(
            select(Appointment, Patient)
            .join(Patient, Appointment.patient_id == Patient.id)
            .where(Appointment.tenant_id == tenant_id)
            .where(Appointment.scheduled_at >= start_utc)
            .where(Appointment.scheduled_at <= end_utc)
            .order_by(Appointment.scheduled_at)
        ).all()
        for ap, pat in rows:
            when_brt = ap.scheduled_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(brt)
            idx = (when_brt.date() - week_start).days
            if 0 <= idx < 7:
                days[idx]["consultas"].append({
                    "id": ap.id,
                    "patient_phone": pat.phone,
                    "patient_name": pat.name or pat.phone,
                    "hora": when_brt.strftime("%H:%M"),
                    "duracao_min": ap.duracao_min,
                    "tipo": ap.tipo.value if hasattr(ap.tipo, "value") else str(ap.tipo),
                    "obs": ap.obs or "",
                    "status": ap.status.value if hasattr(ap.status, "value") else str(ap.status),
                })

    today = datetime.now(brt).date()
    for d in days:
        d["is_today"] = (d["date"] == today)
        d["count"] = len(d["consultas"])

    total = sum(d["count"] for d in days)

    prev_week = (week_start - timedelta(days=7)).isoformat()
    next_week = (week_start + timedelta(days=7)).isoformat()

    return templates.TemplateResponse(
        request,
        "admin/agenda_semana.html",
        {
            "days": days,
            "week_start": week_start,
            "week_end": week_end,
            "week_start_iso": week_start.isoformat(),
            "prev_week": prev_week,
            "next_week": next_week,
            "today_iso": today.isoformat(),
            "total": total,
        },
    )


def _list_active_patients(tenant_id: int) -> list[dict]:
    """Lista pacientes ativas (nome + phone) — usado em selects de form."""
    out: list[dict] = []
    with Session(engine) as db:
        rows = db.exec(
            select(Patient)
            .where(Patient.tenant_id == tenant_id)
            .where(Patient.name.is_not(None))
            .where(Patient.status == PatientStatus.ATIVA)
            .order_by(Patient.name)
        ).all()
        for p in rows:
            out.append({"phone": p.phone, "nome": p.name})
    return out


@router.get("/agenda/nova", response_class=HTMLResponse)
async def admin_agenda_nova_form(
    request: Request,
    paciente: str = "",
    data: str = "",
    tenant_id: int = Depends(verify_admin),
):
    return templates.TemplateResponse(
        request,
        "admin/agenda_form.html",
        {
            "appointment": None,
            "pacientes": _list_active_patients(tenant_id),
            "preset_phone": paciente or "",
            "preset_date": data or "",
            "error": None,
        },
    )


@router.post("/agenda/nova")
async def admin_agenda_nova_submit(
    tenant_id: int = Depends(verify_admin),
    paciente: str = Form(...),
    data_consulta: str = Form(...),
    hora_consulta: str = Form(...),
    duracao_min: int = Form(30),
    tipo: str = Form("consulta"),
    obs: str = Form(""),
):
    brt = ZoneInfo("America/Sao_Paulo")
    cleaned_phone = _clean_phone(paciente)
    if not cleaned_phone:
        raise HTTPException(status_code=400, detail="Telefone da paciente inválido.")

    try:
        d = date.fromisoformat(data_consulta.strip())
        h, m = [int(x) for x in hora_consulta.strip().split(":")[:2]]
        when_brt = datetime(d.year, d.month, d.day, h, m, tzinfo=brt)
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Data/hora inválida.")

    when_utc = when_brt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    with Session(engine) as db:
        patient = db.exec(
            select(Patient)
            .where(Patient.tenant_id == tenant_id)
            .where(Patient.phone == cleaned_phone)
        ).first()
        if not patient:
            raise HTTPException(
                status_code=400,
                detail=f"Paciente com telefone {cleaned_phone} não está cadastrada. Cadastre primeiro.",
            )

        try:
            tipo_enum = AppointmentType(tipo)
        except ValueError:
            tipo_enum = AppointmentType.CONSULTA

        ap = Appointment(
            tenant_id=tenant_id,
            patient_id=patient.id or 0,
            scheduled_at=when_utc,
            duracao_min=max(5, int(duracao_min)),
            tipo=tipo_enum,
            obs=obs.strip() or None,
            created_by="secretaria",
        )
        db.add(ap)
        db.commit()
        db.refresh(ap)
        log.info("agenda nova consulta paciente=%s em %s", cleaned_phone, when_brt.isoformat(timespec="minutes"))

        from .reminders import sync_appointment_reminder
        sync_appointment_reminder(db, ap)

    return RedirectResponse(url=f"/admin/agenda?data={d.isoformat()}", status_code=303)


@router.get("/agenda/{appt_id}/editar", response_class=HTMLResponse)
async def admin_agenda_editar_form(
    appt_id: int,
    request: Request,
    tenant_id: int = Depends(verify_admin),
):
    brt = ZoneInfo("America/Sao_Paulo")
    with Session(engine) as db:
        row = db.exec(
            select(Appointment, Patient)
            .join(Patient, Appointment.patient_id == Patient.id)
            .where(Appointment.id == appt_id)
            .where(Appointment.tenant_id == tenant_id)
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="Consulta não encontrada")
        ap, pat = row
        when_brt = ap.scheduled_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(brt)
        appt_dict = {
            "id": ap.id,
            "patient_phone": pat.phone,
            "patient_name": pat.name or pat.phone,
            "data": when_brt.strftime("%Y-%m-%d"),
            "hora": when_brt.strftime("%H:%M"),
            "duracao_min": ap.duracao_min,
            "tipo": ap.tipo.value if hasattr(ap.tipo, "value") else str(ap.tipo),
            "obs": ap.obs or "",
            "status": ap.status.value if hasattr(ap.status, "value") else str(ap.status),
        }

    return templates.TemplateResponse(
        request,
        "admin/agenda_form.html",
        {
            "appointment": appt_dict,
            "pacientes": [],
            "preset_phone": "",
            "preset_date": "",
            "error": None,
        },
    )


@router.post("/agenda/{appt_id}")
async def admin_agenda_editar_submit(
    appt_id: int,
    tenant_id: int = Depends(verify_admin),
    data_consulta: str = Form(...),
    hora_consulta: str = Form(...),
    duracao_min: int = Form(30),
    tipo: str = Form("consulta"),
    obs: str = Form(""),
    status_consulta: str = Form("agendada"),
):
    brt = ZoneInfo("America/Sao_Paulo")
    try:
        d = date.fromisoformat(data_consulta.strip())
        h, m = [int(x) for x in hora_consulta.strip().split(":")[:2]]
        when_brt = datetime(d.year, d.month, d.day, h, m, tzinfo=brt)
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Data/hora inválida.")
    when_utc = when_brt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    with Session(engine) as db:
        ap = db.exec(
            select(Appointment)
            .where(Appointment.id == appt_id)
            .where(Appointment.tenant_id == tenant_id)
        ).first()
        if not ap:
            raise HTTPException(status_code=404, detail="Consulta não encontrada")

        try:
            ap.tipo = AppointmentType(tipo)
        except ValueError:
            pass
        try:
            new_status = AppointmentStatus(status_consulta)
        except ValueError:
            new_status = ap.status
        ap.status = new_status
        ap.scheduled_at = when_utc
        ap.duracao_min = max(5, int(duracao_min))
        ap.obs = obs.strip() or None
        ap.updated_at = utcnow()
        if new_status == AppointmentStatus.CANCELADA and ap.cancelled_at is None:
            ap.cancelled_at = utcnow()
        db.add(ap)
        db.commit()
        db.refresh(ap)

        from .reminders import sync_appointment_reminder
        sync_appointment_reminder(db, ap)

    return RedirectResponse(url=f"/admin/agenda?data={d.isoformat()}", status_code=303)


@router.post("/agenda/{appt_id}/cancelar")
async def admin_agenda_cancelar(
    appt_id: int,
    tenant_id: int = Depends(verify_admin),
):
    with Session(engine) as db:
        ap = db.exec(
            select(Appointment)
            .where(Appointment.id == appt_id)
            .where(Appointment.tenant_id == tenant_id)
        ).first()
        if not ap:
            raise HTTPException(status_code=404, detail="Consulta não encontrada")
        ap.status = AppointmentStatus.CANCELADA
        ap.cancelled_at = utcnow()
        ap.updated_at = utcnow()
        db.add(ap)
        db.commit()
        db.refresh(ap)
        d_iso = ap.scheduled_at.date().isoformat()

        from .reminders import sync_appointment_reminder
        sync_appointment_reminder(db, ap)

    return RedirectResponse(url=f"/admin/agenda?data={d_iso}", status_code=303)


# =====================================================================
# Lembretes (ScheduledMessage — mensagens automáticas)
# =====================================================================

@router.get("/lembretes", response_class=HTMLResponse)
async def admin_lembretes(
    request: Request,
    data: str = "",
    tenant_id: int = Depends(verify_admin),
):
    brt = ZoneInfo("America/Sao_Paulo")
    target_date = _parse_target_date(data)
    start_utc, end_utc = _day_window_utc(target_date)

    items: list[dict] = []
    with Session(engine) as db:
        rows = db.exec(
            select(ScheduledMessage, Patient)
            .join(Patient, ScheduledMessage.patient_id == Patient.id)
            .where(ScheduledMessage.tenant_id == tenant_id)
            .where(ScheduledMessage.scheduled_at >= start_utc)
            .where(ScheduledMessage.scheduled_at <= end_utc)
            .order_by(ScheduledMessage.scheduled_at)
        ).all()
        for s, pat in rows:
            when_brt = s.scheduled_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(brt)
            if s.cancelled_at:
                st = "cancelado"
            elif s.sent_at:
                st = "enviado"
            else:
                st = "pendente"
            items.append({
                "id": s.id,
                "patient_phone": pat.phone,
                "patient_name": pat.name or pat.phone,
                "hora": when_brt.strftime("%H:%M"),
                "texto": s.text,
                "status": st,
            })

    today = datetime.now(brt).date()
    prev_day = (target_date - timedelta(days=1)).isoformat()
    next_day = (target_date + timedelta(days=1)).isoformat()

    return templates.TemplateResponse(
        request,
        "admin/lembretes.html",
        {
            "items": items,
            "target_date": target_date,
            "target_date_iso": target_date.isoformat(),
            "is_today": target_date == today,
            "prev_day": prev_day,
            "next_day": next_day,
            "today_iso": today.isoformat(),
        },
    )


# =====================================================================
# Ficha individual (paciente)
# =====================================================================

@router.get("/{phone}", response_class=HTMLResponse)
async def admin_edit_form(
    phone: str,
    request: Request,
    tenant_id: int = Depends(verify_admin),
):
    brt = ZoneInfo("America/Sao_Paulo")
    with Session(engine) as db:
        pat = db.exec(
            select(Patient)
            .where(Patient.tenant_id == tenant_id)
            .where(Patient.phone == phone)
        ).first()
        if not pat:
            raise HTTPException(status_code=404, detail="Paciente não encontrada")

        # Consultas (proximas + historico)
        proximas: list[dict] = []
        historico: list[dict] = []
        now_utc = datetime.utcnow()
        rows = db.exec(
            select(Appointment)
            .where(Appointment.tenant_id == tenant_id)
            .where(Appointment.patient_id == pat.id)
            .order_by(Appointment.scheduled_at.desc())
        ).all()
        for ap in rows:
            when_brt = ap.scheduled_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(brt)
            item = {
                "id": ap.id,
                "quando": when_brt.strftime("%d/%m/%Y %H:%M"),
                "tipo": ap.tipo.value if hasattr(ap.tipo, "value") else str(ap.tipo),
                "obs": ap.obs or "",
                "status": ap.status.value if hasattr(ap.status, "value") else str(ap.status),
            }
            status_v = ap.status.value if hasattr(ap.status, "value") else str(ap.status)
            if ap.scheduled_at >= now_utc and status_v in ("agendada", "confirmada"):
                proximas.append(item)
            else:
                historico.append(item)
        proximas.reverse()

    return templates.TemplateResponse(
        request,
        "admin/form.html",
        {
            "patient": _patient_to_dict(pat),
            "phone_lock": True,
            "error": None,
            "consultas_proximas": proximas,
            "consultas_historico": historico,
        },
    )


# =====================================================================
# Salvar paciente (criar/atualizar)
# =====================================================================

def _apply_form_to_patient(p: Patient, form: dict) -> None:
    """Copia valores do form pro modelo Patient (in-place)."""
    p.name = (form.get("nome") or "").strip() or None
    p.data_nascimento = _parse_date(form.get("data_nascimento"))
    p.endereco = (form.get("endereco") or "").strip() or None
    p.dum = _parse_date(form.get("dum"))
    p.tipo_gestacao = (form.get("tipo_gestacao") or "unica").strip()
    p.risco = (form.get("risco") or "habitual").strip()
    p.gestacao_planejada = bool(form.get("gestacao_planejada"))
    p.gestas = _parse_int(form.get("gestas"), 0)
    p.partos_normais = _parse_int(form.get("partos_normais"), 0)
    p.cesareas = _parse_int(form.get("cesareas"), 0)
    p.abortos = _parse_int(form.get("abortos"), 0)
    p.alergias = (form.get("alergias") or "").strip() or None
    p.condicoes_pre_existentes = (form.get("condicoes_pre_existentes") or "").strip() or None
    p.medicacoes_em_uso = (form.get("medicacoes_em_uso") or "").strip() or None
    p.grupo_sanguineo = (form.get("grupo_sanguineo") or "").strip() or None
    p.medico_obstetra = (form.get("medico_obstetra") or settings.doctor_name).strip() or None
    p.hospital_referencia = (form.get("hospital_referencia") or "").strip() or None
    p.plano_saude = (form.get("plano_saude") or "").strip() or None
    p.contato_emergencia_nome = (form.get("contato_emergencia_nome") or "").strip() or None
    contato_tel = form.get("contato_emergencia_telefone") or ""
    p.contato_emergencia_telefone = _clean_phone(contato_tel) or (contato_tel.strip() or None)
    p.contato_emergencia_relacao = (form.get("contato_emergencia_relacao") or "").strip() or None
    p.preferencias_atendimento = (form.get("preferencias_atendimento") or "").strip() or None
    p.historico_clinico = (form.get("historico_clinico") or "").strip() or None
    p.historico_obstetrico = (form.get("historico_obstetrico") or "").strip() or None
    p.observacoes_dra = (form.get("observacoes_dra") or "").strip() or None
    p.updated_at = utcnow()


async def _save_patient(phone_url: str | None, form: dict, tenant_id: int) -> RedirectResponse:
    cleaned_phone = _clean_phone(form["telefone"])
    if not cleaned_phone:
        raise HTTPException(status_code=400, detail="Telefone inválido. Use formato E.164 sem +, ex: 5511999990001")
    if phone_url and phone_url != cleaned_phone:
        raise HTTPException(status_code=400, detail="Não é permitido alterar o telefone. Crie uma nova ficha se necessário.")

    with Session(engine) as db:
        existing = db.exec(
            select(Patient)
            .where(Patient.tenant_id == tenant_id)
            .where(Patient.phone == cleaned_phone)
        ).first()

        if existing:
            _apply_form_to_patient(existing, form)
            db.add(existing)
        else:
            new_patient = Patient(
                tenant_id=tenant_id,
                phone=cleaned_phone,
                onboarding_state=OnboardingState.DONE,  # cadastro pelo painel = onboarding completo
            )
            _apply_form_to_patient(new_patient, form)
            db.add(new_patient)
        db.commit()

    return RedirectResponse(url=f"/admin/?ok={cleaned_phone}", status_code=303)


@router.post("/novo")
async def admin_novo_submit(
    tenant_id: int = Depends(verify_admin),
    nome: str = Form(...),
    telefone: str = Form(...),
    data_nascimento: str = Form(""),
    endereco: str = Form(""),
    dum: str = Form(""),
    tipo_gestacao: str = Form("unica"),
    risco: str = Form("habitual"),
    gestacao_planejada: bool = Form(False),
    gestas: int = Form(0),
    partos_normais: int = Form(0),
    cesareas: int = Form(0),
    abortos: int = Form(0),
    alergias: str = Form(""),
    condicoes_pre_existentes: str = Form(""),
    medicacoes_em_uso: str = Form(""),
    grupo_sanguineo: str = Form(""),
    medico_obstetra: str = Form(""),
    hospital_referencia: str = Form(""),
    plano_saude: str = Form(""),
    contato_emergencia_nome: str = Form(""),
    contato_emergencia_telefone: str = Form(""),
    contato_emergencia_relacao: str = Form(""),
    preferencias_atendimento: str = Form(""),
    historico_clinico: str = Form(""),
    historico_obstetrico: str = Form(""),
    observacoes_dra: str = Form(""),
):
    return await _save_patient(None, locals(), tenant_id)


@router.post("/{phone}")
async def admin_edit_submit(
    phone: str,
    tenant_id: int = Depends(verify_admin),
    nome: str = Form(...),
    telefone: str = Form(...),
    data_nascimento: str = Form(""),
    endereco: str = Form(""),
    dum: str = Form(""),
    tipo_gestacao: str = Form("unica"),
    risco: str = Form("habitual"),
    gestacao_planejada: bool = Form(False),
    gestas: int = Form(0),
    partos_normais: int = Form(0),
    cesareas: int = Form(0),
    abortos: int = Form(0),
    alergias: str = Form(""),
    condicoes_pre_existentes: str = Form(""),
    medicacoes_em_uso: str = Form(""),
    grupo_sanguineo: str = Form(""),
    medico_obstetra: str = Form(""),
    hospital_referencia: str = Form(""),
    plano_saude: str = Form(""),
    contato_emergencia_nome: str = Form(""),
    contato_emergencia_telefone: str = Form(""),
    contato_emergencia_relacao: str = Form(""),
    preferencias_atendimento: str = Form(""),
    historico_clinico: str = Form(""),
    historico_obstetrico: str = Form(""),
    observacoes_dra: str = Form(""),
):
    return await _save_patient(phone, locals(), tenant_id)
