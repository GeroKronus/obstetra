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
