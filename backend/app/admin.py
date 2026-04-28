"""Interface web admin para a secretária cadastrar/editar pacientes.

Rotas:
- GET  /admin/         — lista de pacientes
- GET  /admin/novo     — formulário em branco
- POST /admin/novo     — cria paciente
- GET  /admin/{phone}  — formulário pré-preenchido
- POST /admin/{phone}  — atualiza paciente

Autenticação: HTTP Basic via env vars ADMIN_USER e ADMIN_PASSWORD.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import frontmatter
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import vault as vault_module
from .config import settings

log = logging.getLogger("obstetra.admin")
security = HTTPBasic()

_BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

router = APIRouter(prefix="/admin")


def verify_admin(credentials: Annotated[HTTPBasicCredentials, Depends(security)]) -> str:
    if not settings.admin_user or not settings.admin_password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin não configurado (ADMIN_USER/ADMIN_PASSWORD ausentes).",
        )
    correct_user = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.admin_user.encode("utf-8"),
    )
    correct_pass = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.admin_password.encode("utf-8"),
    )
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciais inválidas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def _vault_pacientes_path() -> Path:
    return Path(settings.vault_local_path) / "pacientes"


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _join_lines(items: list | None) -> str:
    if not items:
        return ""
    return "\n".join(str(x) for x in items)


def _date_str(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value)


def _parse_dum(value: Any) -> date | None:
    """Aceita date, datetime ou string ISO 'YYYY-MM-DD'."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _extract_section(body: str, heading: str) -> str:
    if not body:
        return ""
    target = f"## {heading}".lower()
    out: list[str] = []
    capturing = False
    for line in body.splitlines():
        if line.strip().lower().startswith("## "):
            if capturing:
                break
            if line.strip().lower() == target:
                capturing = True
                continue
        if capturing:
            out.append(line)
    return "\n".join(out).strip()


def _load_patient(phone: str) -> dict | None:
    """Lê pacientes/<phone>/anamnese.md e retorna como dict pronto pra template."""
    path = _vault_pacientes_path() / phone / "anamnese.md"
    if not path.exists():
        return None
    try:
        post = frontmatter.load(path)
    except Exception:
        log.exception("falha ao parsear %s", path)
        return None
    fm = post.metadata or {}
    body = post.content or ""
    return {
        "nome": fm.get("nome", ""),
        "telefone": fm.get("telefone", phone),
        "data_nascimento": _date_str(fm.get("data_nascimento")),
        "endereco": fm.get("endereco", ""),
        "dum": _date_str(fm.get("dum")),
        "tipo_gestacao": fm.get("tipo_gestacao", "unica"),
        "risco": fm.get("risco", "habitual"),
        "gestacao_planejada": bool(fm.get("gestacao_planejada", False)),
        "gestas": fm.get("gestas", 0) or 0,
        "partos_normais": fm.get("partos_normais", 0) or 0,
        "cesareas": fm.get("cesareas", 0) or 0,
        "abortos": fm.get("abortos", 0) or 0,
        "alergias": _join_lines(fm.get("alergias")),
        "condicoes_pre_existentes": _join_lines(fm.get("condicoes_pre_existentes")),
        "medicacoes_em_uso": _join_lines(fm.get("medicacoes_em_uso")),
        "grupo_sanguineo": fm.get("grupo_sanguineo", ""),
        "medico_obstetra": fm.get("medico_obstetra", "Dra. Leiza"),
        "hospital_referencia": fm.get("hospital_referencia", ""),
        "plano_saude": fm.get("plano_saude", ""),
        "contato_emergencia_nome": fm.get("contato_emergencia_nome", ""),
        "contato_emergencia_telefone": fm.get("contato_emergencia_telefone", ""),
        "contato_emergencia_relacao": fm.get("contato_emergencia_relacao", ""),
        "preferencias_atendimento": fm.get("preferencias_atendimento", ""),
        "historico_clinico": _extract_section(body, "Histórico clínico relevante"),
        "historico_obstetrico": _extract_section(body, "Histórico obstétrico"),
        "observacoes_dra": _extract_section(body, "Observações pessoais da Dra."),
        "status": fm.get("status", "ativa"),
    }


def _build_anamnese_md(data: dict) -> str:
    """Monta o markdown completo (frontmatter + body) a partir do dict da requisição."""
    now = datetime.now().isoformat(timespec="seconds")
    # Converte datas pra objeto date — assim PyYAML escreve sem aspas
    # (ex: 'dum: 2025-10-08' em vez de "dum: '2025-10-08'"), o que faz a
    # leitura subsequente funcionar transparentemente.
    fm = {
        "nome": data["nome"],
        "telefone": data["telefone"],
        "data_nascimento": _parse_dum(data["data_nascimento"]) if data["data_nascimento"] else None,
        "endereco": data["endereco"],
        "dum": _parse_dum(data["dum"]) if data["dum"] else None,
        "tipo_gestacao": data["tipo_gestacao"],
        "risco": data["risco"],
        "gestacao_planejada": data["gestacao_planejada"],
        "gestas": data["gestas"],
        "partos_normais": data["partos_normais"],
        "cesareas": data["cesareas"],
        "abortos": data["abortos"],
        "alergias": _split_lines(data["alergias"]),
        "condicoes_pre_existentes": _split_lines(data["condicoes_pre_existentes"]),
        "medicacoes_em_uso": _split_lines(data["medicacoes_em_uso"]),
        "grupo_sanguineo": data["grupo_sanguineo"],
        "medico_obstetra": data["medico_obstetra"] or "Dra. Leiza",
        "hospital_referencia": data["hospital_referencia"],
        "plano_saude": data["plano_saude"],
        "contato_emergencia_nome": data.get("contato_emergencia_nome", ""),
        "contato_emergencia_telefone": _clean_phone(data.get("contato_emergencia_telefone", "") or "") or "",
        "contato_emergencia_relacao": data.get("contato_emergencia_relacao", ""),
        "status": data.get("status", "ativa"),
        "preferencias_atendimento": data["preferencias_atendimento"],
        "created_at": data.get("created_at", now),
        "updated_at": now,
    }
    body = (
        f"# {data['nome']}\n\n"
        f"## Histórico clínico relevante\n\n{data['historico_clinico'].strip()}\n\n"
        f"## Histórico obstétrico\n\n{data['historico_obstetrico'].strip()}\n\n"
        f"## Observações pessoais da Dra.\n\n{data['observacoes_dra'].strip()}\n"
    )
    post = frontmatter.Post(content=body, **fm)
    return frontmatter.dumps(post, allow_unicode=True, sort_keys=False)


def _clean_phone(raw: str) -> str | None:
    """Normaliza telefone pro formato E.164 sem `+` (ex: 5528988030050).

    Aceita como input:
    - Apenas DDD + número (10-11 dígitos): adiciona '55' na frente
    - Já com código do país (12-13 dígitos começando com 55): mantém
    - Qualquer um com formatação ((28) 9 9999-9999, +55 28..., etc.)

    Retorna E.164 sem `+`, ou None se não der pra normalizar."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    # Já tem código do país
    if digits.startswith("55") and 12 <= len(digits) <= 13:
        return digits
    # DDD + número (cellphone com 9 = 11 dígitos; landline = 10 dígitos)
    if 10 <= len(digits) <= 11:
        return "55" + digits
    return None


def _build_search_blob(fm: dict, body: str) -> str:
    """Concatena todos os campos pesquisaveis num unico string normalizado em lowercase."""
    parts: list[str] = []

    def _add(value):
        if not value:
            return
        if isinstance(value, list):
            parts.extend(str(v) for v in value if v)
        else:
            parts.append(str(value))

    for key in (
        "nome", "telefone", "data_nascimento", "endereco",
        "tipo_gestacao", "risco", "grupo_sanguineo",
        "alergias", "condicoes_pre_existentes", "medicacoes_em_uso",
        "medico_obstetra", "hospital_referencia", "plano_saude",
        "contato_emergencia_nome", "contato_emergencia_telefone",
        "contato_emergencia_relacao",
        "preferencias_atendimento", "status",
    ):
        _add(fm.get(key))

    if body:
        parts.append(body)

    return " ".join(parts).lower()


@router.get("/", response_class=HTMLResponse)
async def admin_index(
    request: Request,
    q: str = "",
    _: str = Depends(verify_admin),
):
    # Pull antes de listar pra ter dados frescos
    try:
        await vault_module._pull_if_stale()
    except Exception:
        log.exception("pull no /admin/ falhou — listando do cache local")

    query = (q or "").strip().lower()
    pacientes_dir = _vault_pacientes_path()
    patients: list[dict] = []
    total = 0

    if pacientes_dir.exists():
        for d in sorted(pacientes_dir.iterdir()):
            if not d.is_dir():
                continue
            anamnese = d / "anamnese.md"
            if not anamnese.exists():
                continue
            try:
                post = frontmatter.load(anamnese)
                fm = post.metadata or {}
                body = post.content or ""
            except Exception:
                log.exception("erro lendo %s", anamnese)
                continue

            total += 1

            # Filtra ANTES de montar o card (evita custo de renderizar o que vai sumir)
            if query:
                blob = _build_search_blob(fm, body)
                if query not in blob:
                    continue

            dum = _parse_dum(fm.get("dum"))
            semanas: int | None = None
            if dum:
                delta = (date.today() - dum).days
                semanas = delta // 7 if delta >= 0 else None

            patients.append({
                "phone": d.name,
                "nome": fm.get("nome") or d.name,
                "semanas": semanas,
                "tipo": fm.get("tipo_gestacao", "-"),
                "risco": fm.get("risco", "-"),
                "status": fm.get("status", "ativa"),
            })

    return templates.TemplateResponse(
        request,
        "admin/list.html",
        {"patients": patients, "q": q, "total": total},
    )


def _day_window_utc(target_date: date) -> tuple[datetime, datetime]:
    """Retorna (start_utc, end_utc) naive cobrindo BRT 00:00 a 23:59 do dia."""
    from zoneinfo import ZoneInfo
    brt = ZoneInfo("America/Sao_Paulo")
    start_brt = datetime.combine(target_date, datetime.min.time(), tzinfo=brt)
    end_brt = datetime.combine(target_date, datetime.max.time(), tzinfo=brt)
    return (
        start_brt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
        end_brt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None),
    )


def _parse_target_date(data: str) -> date:
    """Parseia ?data=YYYY-MM-DD; fallback pra hoje BRT."""
    from zoneinfo import ZoneInfo
    brt = ZoneInfo("America/Sao_Paulo")
    if data:
        try:
            return date.fromisoformat(data.strip())
        except ValueError:
            pass
    return datetime.now(brt).date()


@router.get("/agenda", response_class=HTMLResponse)
async def admin_agenda(
    request: Request,
    data: str = "",
    _: str = Depends(verify_admin),
):
    """Visao do dia da agenda de consultas."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from sqlmodel import Session, select
    from .db import engine
    from .models import Appointment, Patient

    brt = ZoneInfo("America/Sao_Paulo")
    target_date = _parse_target_date(data)
    start_utc, end_utc = _day_window_utc(target_date)

    items: list[dict] = []
    with Session(engine) as db:
        rows = db.exec(
            select(Appointment, Patient)
            .join(Patient, Appointment.patient_id == Patient.id)
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


@router.get("/agenda/nova", response_class=HTMLResponse)
async def admin_agenda_nova_form(
    request: Request,
    paciente: str = "",
    data: str = "",
    _: str = Depends(verify_admin),
):
    """Form de nova consulta. Aceita ?paciente=<phone>&data=YYYY-MM-DD pra pre-preencher."""
    pacientes = _list_active_patients()
    return templates.TemplateResponse(
        request,
        "admin/agenda_form.html",
        {
            "appointment": None,
            "pacientes": pacientes,
            "preset_phone": paciente or "",
            "preset_date": data or "",
            "error": None,
        },
    )


@router.post("/agenda/nova")
async def admin_agenda_nova_submit(
    _: str = Depends(verify_admin),
    paciente: str = Form(...),
    data_consulta: str = Form(...),
    hora_consulta: str = Form(...),
    duracao_min: int = Form(30),
    tipo: str = Form("consulta"),
    obs: str = Form(""),
):
    from zoneinfo import ZoneInfo
    from sqlmodel import Session, select
    from .db import engine
    from .models import Appointment, AppointmentType, Patient

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

    # Verifica se paciente existe no vault (ficha)
    vault_anamnese = _vault_pacientes_path() / cleaned_phone / "anamnese.md"
    if not vault_anamnese.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Paciente com telefone {cleaned_phone} não está cadastrada. Cadastre primeiro em /admin/novo.",
        )

    with Session(engine) as db:
        patient = db.exec(select(Patient).where(Patient.phone == cleaned_phone)).first()
        if not patient:
            # Cria Patient no DB a partir da ficha do vault — secretaria
            # pode cadastrar e ja agendar antes da paciente conversar
            from .models import OnboardingState
            try:
                fm = frontmatter.load(vault_anamnese).metadata or {}
            except Exception:
                fm = {}
            patient = Patient(
                phone=cleaned_phone,
                name=fm.get("nome") or None,
                onboarding_state=OnboardingState.DONE,
            )
            db.add(patient)
            db.commit()
            db.refresh(patient)

        try:
            tipo_enum = AppointmentType(tipo)
        except ValueError:
            tipo_enum = AppointmentType.CONSULTA

        ap = Appointment(
            patient_id=patient.id or 0,
            scheduled_at=when_utc,
            duracao_min=max(5, int(duracao_min)),
            tipo=tipo_enum,
            obs=obs.strip() or None,
            created_by="secretaria",
        )
        db.add(ap)
        db.commit()
        log.info("agenda nova consulta paciente=%s em %s", cleaned_phone, when_brt.isoformat(timespec="minutes"))

    return RedirectResponse(url=f"/admin/agenda?data={d.isoformat()}", status_code=303)


@router.get("/agenda/{appt_id}/editar", response_class=HTMLResponse)
async def admin_agenda_editar_form(
    appt_id: int,
    request: Request,
    _: str = Depends(verify_admin),
):
    from zoneinfo import ZoneInfo
    from sqlmodel import Session, select
    from .db import engine
    from .models import Appointment, Patient

    brt = ZoneInfo("America/Sao_Paulo")
    with Session(engine) as db:
        row = db.exec(
            select(Appointment, Patient)
            .join(Patient, Appointment.patient_id == Patient.id)
            .where(Appointment.id == appt_id)
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
    _: str = Depends(verify_admin),
    data_consulta: str = Form(...),
    hora_consulta: str = Form(...),
    duracao_min: int = Form(30),
    tipo: str = Form("consulta"),
    obs: str = Form(""),
    status_consulta: str = Form("agendada"),
):
    from zoneinfo import ZoneInfo
    from sqlmodel import Session, select
    from .db import engine
    from .models import Appointment, AppointmentStatus, AppointmentType, utcnow

    brt = ZoneInfo("America/Sao_Paulo")
    try:
        d = date.fromisoformat(data_consulta.strip())
        h, m = [int(x) for x in hora_consulta.strip().split(":")[:2]]
        when_brt = datetime(d.year, d.month, d.day, h, m, tzinfo=brt)
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Data/hora inválida.")
    when_utc = when_brt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    with Session(engine) as db:
        ap = db.exec(select(Appointment).where(Appointment.id == appt_id)).first()
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

    return RedirectResponse(url=f"/admin/agenda?data={d.isoformat()}", status_code=303)


@router.post("/agenda/{appt_id}/cancelar")
async def admin_agenda_cancelar(
    appt_id: int,
    _: str = Depends(verify_admin),
):
    from sqlmodel import Session, select
    from .db import engine
    from .models import Appointment, AppointmentStatus, utcnow

    with Session(engine) as db:
        ap = db.exec(select(Appointment).where(Appointment.id == appt_id)).first()
        if not ap:
            raise HTTPException(status_code=404, detail="Consulta não encontrada")
        ap.status = AppointmentStatus.CANCELADA
        ap.cancelled_at = utcnow()
        ap.updated_at = utcnow()
        db.add(ap)
        db.commit()
        d_iso = ap.scheduled_at.date().isoformat()

    return RedirectResponse(url=f"/admin/agenda?data={d_iso}", status_code=303)


@router.get("/lembretes", response_class=HTMLResponse)
async def admin_lembretes(
    request: Request,
    data: str = "",
    _: str = Depends(verify_admin),
):
    """Lista lembretes (ScheduledMessage) por dia — diferente de consultas."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from sqlmodel import Session, select
    from .db import engine
    from .models import ScheduledMessage, Patient

    brt = ZoneInfo("America/Sao_Paulo")
    target_date = _parse_target_date(data)
    start_utc, end_utc = _day_window_utc(target_date)

    items: list[dict] = []
    with Session(engine) as db:
        rows = db.exec(
            select(ScheduledMessage, Patient)
            .join(Patient, ScheduledMessage.patient_id == Patient.id)
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


def _list_active_patients() -> list[dict]:
    """Le vault e retorna pacientes ativas (nome + phone) pra select do form."""
    pacientes_dir = _vault_pacientes_path()
    out: list[dict] = []
    if not pacientes_dir.exists():
        return out
    for d in sorted(pacientes_dir.iterdir()):
        if not d.is_dir():
            continue
        anamnese = d / "anamnese.md"
        if not anamnese.exists():
            continue
        try:
            post = frontmatter.load(anamnese)
            fm = post.metadata or {}
        except Exception:
            continue
        if fm.get("status") and fm.get("status") != "ativa":
            continue
        out.append({"phone": d.name, "nome": fm.get("nome") or d.name})
    return out


@router.get("/novo", response_class=HTMLResponse)
async def admin_novo_form(request: Request, _: str = Depends(verify_admin)):
    return templates.TemplateResponse(
        request,
        "admin/form.html",
        {"patient": None, "phone_lock": False, "error": None},
    )


@router.get("/{phone}", response_class=HTMLResponse)
async def admin_edit_form(phone: str, request: Request, _: str = Depends(verify_admin)):
    from zoneinfo import ZoneInfo
    from sqlmodel import Session, select
    from .db import engine
    from .models import Appointment, Patient

    patient = _load_patient(phone)
    if not patient:
        raise HTTPException(status_code=404, detail="Paciente não encontrada")

    # Carrega consultas (proximas + historico)
    proximas: list[dict] = []
    historico: list[dict] = []
    brt = ZoneInfo("America/Sao_Paulo")
    now_utc = datetime.utcnow()
    with Session(engine) as db:
        pat_row = db.exec(select(Patient).where(Patient.phone == phone)).first()
        if pat_row:
            rows = db.exec(
                select(Appointment)
                .where(Appointment.patient_id == pat_row.id)
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
                # Proximas: agendada/confirmada e no futuro
                if ap.scheduled_at >= now_utc and ap.status.value in ("agendada", "confirmada"):
                    proximas.append(item)
                else:
                    historico.append(item)
            proximas.reverse()  # cronologica ascendente

    return templates.TemplateResponse(
        request,
        "admin/form.html",
        {
            "patient": patient,
            "phone_lock": True,
            "error": None,
            "consultas_proximas": proximas,
            "consultas_historico": historico,
        },
    )


async def _save_patient_form(phone_url: str | None, form: dict) -> RedirectResponse:
    cleaned_phone = _clean_phone(form["telefone"])
    if not cleaned_phone:
        raise HTTPException(status_code=400, detail="Telefone inválido. Use formato E.164 sem +, ex: 5511999990001")
    if phone_url and phone_url != cleaned_phone:
        # Telefone mudou em uma edição — bloqueia (renomear pasta é intrusivo)
        raise HTTPException(status_code=400, detail="Não é permitido alterar o telefone. Crie uma nova ficha se necessário.")

    target_dir = _vault_pacientes_path() / cleaned_phone
    is_new = not (target_dir / "anamnese.md").exists()
    target_dir.mkdir(parents=True, exist_ok=True)

    # Preserva created_at se já existir
    if not is_new:
        try:
            existing = frontmatter.load(target_dir / "anamnese.md").metadata or {}
            form["created_at"] = existing.get("created_at") or datetime.now().isoformat(timespec="seconds")
            form["status"] = existing.get("status", "ativa")
        except Exception:
            pass

    form["telefone"] = cleaned_phone
    md = _build_anamnese_md(form)
    (target_dir / "anamnese.md").write_text(md, encoding="utf-8")

    verb = "cadastro" if is_new else "atualização"
    await vault_module.commit_and_push(f"admin: {verb} de {form['nome']} ({cleaned_phone})")

    return RedirectResponse(url=f"/admin/?ok={cleaned_phone}", status_code=303)


@router.post("/novo")
async def admin_novo_submit(
    _: str = Depends(verify_admin),
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
    medico_obstetra: str = Form("Dra. Leiza"),
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
    return await _save_patient_form(None, locals())


@router.post("/{phone}")
async def admin_edit_submit(
    phone: str,
    _: str = Depends(verify_admin),
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
    medico_obstetra: str = Form("Dra. Leiza"),
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
    return await _save_patient_form(phone, locals())
