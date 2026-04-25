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
        "preferencias_atendimento": fm.get("preferencias_atendimento", ""),
        "historico_clinico": _extract_section(body, "Histórico clínico relevante"),
        "historico_obstetrico": _extract_section(body, "Histórico obstétrico"),
        "observacoes_dra": _extract_section(body, "Observações pessoais da Dra."),
        "status": fm.get("status", "ativa"),
    }


def _build_anamnese_md(data: dict) -> str:
    """Monta o markdown completo (frontmatter + body) a partir do dict da requisição."""
    now = datetime.now().isoformat(timespec="seconds")
    fm = {
        "nome": data["nome"],
        "telefone": data["telefone"],
        "data_nascimento": data["data_nascimento"] or None,
        "endereco": data["endereco"],
        "dum": data["dum"] or None,
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
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 10 or len(digits) > 15:
        return None
    return digits


@router.get("/", response_class=HTMLResponse)
async def admin_index(request: Request, _: str = Depends(verify_admin)):
    # Pull antes de listar pra ter dados frescos
    try:
        await vault_module._pull_if_stale()
    except Exception:
        log.exception("pull no /admin/ falhou — listando do cache local")

    pacientes_dir = _vault_pacientes_path()
    patients: list[dict] = []
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
                dum = fm.get("dum")
                semanas: int | None = None
                if dum:
                    if isinstance(dum, datetime):
                        dum = dum.date()
                    if isinstance(dum, date):
                        delta = (date.today() - dum).days
                        semanas = delta // 7 if delta >= 0 else None
                patients.append(
                    {
                        "phone": d.name,
                        "nome": fm.get("nome") or d.name,
                        "semanas": semanas,
                        "tipo": fm.get("tipo_gestacao", "-"),
                        "risco": fm.get("risco", "-"),
                        "status": fm.get("status", "ativa"),
                    }
                )
            except Exception:
                log.exception("erro lendo %s", anamnese)

    return templates.TemplateResponse(
        request,
        "admin/list.html",
        {"patients": patients},
    )


@router.get("/novo", response_class=HTMLResponse)
async def admin_novo_form(request: Request, _: str = Depends(verify_admin)):
    return templates.TemplateResponse(
        request,
        "admin/form.html",
        {"patient": None, "phone_lock": False, "error": None},
    )


@router.get("/{phone}", response_class=HTMLResponse)
async def admin_edit_form(phone: str, request: Request, _: str = Depends(verify_admin)):
    patient = _load_patient(phone)
    if not patient:
        raise HTTPException(status_code=404, detail="Paciente não encontrada")
    return templates.TemplateResponse(
        request,
        "admin/form.html",
        {"patient": patient, "phone_lock": True, "error": None},
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
    preferencias_atendimento: str = Form(""),
    historico_clinico: str = Form(""),
    historico_obstetrico: str = Form(""),
    observacoes_dra: str = Form(""),
):
    return await _save_patient_form(phone, locals())
