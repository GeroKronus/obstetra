"""Integração com o vault Obsidian (repositório Git).

- Clona o vault no startup (se ainda não existir localmente).
- Pull leve antes de cada leitura (com TTL pra evitar pull em rajada).
- Lê `pacientes/<telefone>/anamnese.md` e parseia o frontmatter.
- Append em `pacientes/<telefone>/conversas.md` com commit + push.

Toda operação de git é serializada por um asyncio.Lock pra evitar conflitos
de file lock e race conditions entre webhooks concorrentes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter

from .config import settings

log = logging.getLogger("obstetra.vault")

_lock = asyncio.Lock()
_last_pull_at: float = 0.0
_ssh_key_path: Path | None = None


def _is_enabled() -> bool:
    return bool(settings.vault_repo_url and settings.vault_ssh_private_key)


def _setup_ssh_key() -> str | None:
    """Escreve a chave SSH em disco e retorna o GIT_SSH_COMMAND a usar."""
    global _ssh_key_path
    if not settings.vault_ssh_private_key:
        return None
    if _ssh_key_path and _ssh_key_path.exists():
        return _git_ssh_command(_ssh_key_path)

    key_dir = Path("/tmp/obstetra-ssh")
    key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = key_dir / "vault_deploy_key"
    key_path.write_text(settings.vault_ssh_private_key, encoding="utf-8", newline="\n")
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    _ssh_key_path = key_path
    log.info("vault SSH key written to %s", key_path)
    return _git_ssh_command(key_path)


def _git_ssh_command(key_path: Path) -> str:
    return (
        f"ssh -i {key_path} "
        "-o IdentitiesOnly=yes "
        "-o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null"
    )


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    cmd = _setup_ssh_key()
    if cmd:
        env["GIT_SSH_COMMAND"] = cmd
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("git %s (cwd=%s)", " ".join(args), cwd)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and proc.returncode != 0:
        log.error("git %s failed (exit %d): stdout=%s stderr=%s",
                  " ".join(args), proc.returncode, proc.stdout, proc.stderr)
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc


def _vault_path() -> Path:
    return Path(settings.vault_local_path)


async def init_vault() -> None:
    """Clona o vault no startup se ainda não estiver presente."""
    if not _is_enabled():
        log.info("vault disabled (VAULT_REPO_URL or VAULT_SSH_PRIVATE_KEY ausente) — bot funciona sem contexto da paciente")
        return

    async with _lock:
        path = _vault_path()
        if (path / ".git").exists():
            log.info("vault já clonado em %s — fazendo pull inicial", path)
            try:
                _run_git(["pull", "--ff-only"], cwd=path)
            except Exception:
                log.exception("pull inicial falhou (vai tentar de novo na próxima leitura)")
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.rmtree(path)
        log.info("clonando vault em %s", path)
        _run_git(["clone", settings.vault_repo_url, str(path)])
        _run_git(["config", "user.name", settings.vault_git_user_name], cwd=path)
        _run_git(["config", "user.email", settings.vault_git_user_email], cwd=path)
        log.info("vault clonado com sucesso")


async def _pull_if_stale() -> None:
    global _last_pull_at
    now = time.time()
    if now - _last_pull_at < settings.vault_pull_max_age_s:
        return
    path = _vault_path()
    if not (path / ".git").exists():
        return
    try:
        _run_git(["pull", "--ff-only"], cwd=path)
        _last_pull_at = now
    except Exception:
        log.exception("vault pull falhou (mantendo cache local)")


@dataclass
class PatientContext:
    """Dados estruturados que o bot lê do vault para contextualizar o atendimento."""
    found: bool
    nome: str | None = None
    semanas_atuais: int | None = None
    tipo_gestacao: str | None = None
    risco: str | None = None
    data_provavel_parto: str | None = None
    alergias: list[str] | None = None
    condicoes_pre_existentes: list[str] | None = None
    medicacoes_em_uso: list[str] | None = None
    preferencias_atendimento: str | None = None
    historico_clinico: str | None = None
    historico_obstetrico: str | None = None
    observacoes_dra: str | None = None

    def to_prompt_block(self) -> str:
        """Renderiza como bloco XML-style pro prompt do agente."""
        if not self.found:
            return "<paciente_no_vault>nao_encontrada</paciente_no_vault>"

        lines = ["<paciente_no_vault>"]
        if self.nome: lines.append(f"  nome: {self.nome}")
        if self.semanas_atuais is not None: lines.append(f"  semanas_gestacao: {self.semanas_atuais}")
        if self.tipo_gestacao: lines.append(f"  tipo_gestacao: {self.tipo_gestacao}")
        if self.risco: lines.append(f"  risco: {self.risco}")
        if self.data_provavel_parto: lines.append(f"  data_provavel_parto: {self.data_provavel_parto}")
        if self.alergias: lines.append(f"  alergias: {', '.join(self.alergias)}")
        if self.condicoes_pre_existentes:
            lines.append(f"  condicoes_pre_existentes: {', '.join(self.condicoes_pre_existentes)}")
        if self.medicacoes_em_uso:
            lines.append(f"  medicacoes_em_uso: {', '.join(self.medicacoes_em_uso)}")
        if self.preferencias_atendimento: lines.append(f"  preferencias: {self.preferencias_atendimento}")
        if self.observacoes_dra:
            lines.append("  observacoes_da_doutora: |")
            for line in self.observacoes_dra.splitlines():
                lines.append(f"    {line}")
        if self.historico_clinico:
            lines.append("  historico_clinico: |")
            for line in self.historico_clinico.splitlines():
                lines.append(f"    {line}")
        if self.historico_obstetrico:
            lines.append("  historico_obstetrico: |")
            for line in self.historico_obstetrico.splitlines():
                lines.append(f"    {line}")
        lines.append("</paciente_no_vault>")
        return "\n".join(lines)


def _extract_section(body: str, heading: str) -> str | None:
    """Extrai texto de uma seção (## Heading) até a próxima seção de mesmo nível."""
    if not body:
        return None
    lines = body.splitlines()
    target = f"## {heading}".lower()
    out: list[str] = []
    capturing = False
    for line in lines:
        stripped = line.strip().lower()
        if stripped.startswith("## "):
            if capturing:
                break
            if stripped == target:
                capturing = True
                continue
        if capturing:
            out.append(line)
    text = "\n".join(out).strip()
    # remove os placeholders italicizados típicos do template
    if text.startswith("*(") and text.endswith(")*") and "\n" not in text:
        return None
    return text or None


async def read_patient(phone: str) -> PatientContext:
    if not _is_enabled():
        return PatientContext(found=False)

    async with _lock:
        await _pull_if_stale()
        anamnese = _vault_path() / "pacientes" / phone / "anamnese.md"
        if not anamnese.exists():
            return PatientContext(found=False)
        try:
            post = frontmatter.load(anamnese)
        except Exception:
            log.exception("falha ao parsear %s", anamnese)
            return PatientContext(found=False)

    fm: dict[str, Any] = post.metadata or {}
    body: str = post.content or ""

    def _list(field: str) -> list[str] | None:
        value = fm.get(field)
        if not value:
            return None
        if isinstance(value, list):
            return [str(x) for x in value if x]
        return [str(value)]

    return PatientContext(
        found=True,
        nome=fm.get("nome"),
        semanas_atuais=fm.get("semanas_atuais"),
        tipo_gestacao=fm.get("tipo_gestacao"),
        risco=fm.get("risco"),
        data_provavel_parto=str(fm.get("data_provavel_parto")) if fm.get("data_provavel_parto") else None,
        alergias=_list("alergias"),
        condicoes_pre_existentes=_list("condicoes_pre_existentes"),
        medicacoes_em_uso=_list("medicacoes_em_uso"),
        preferencias_atendimento=fm.get("preferencias_atendimento") or None,
        historico_clinico=_extract_section(body, "Histórico clínico relevante"),
        historico_obstetrico=_extract_section(body, "Histórico obstétrico"),
        observacoes_dra=_extract_section(body, "Observações pessoais da Dra."),
    )


async def append_conversation(
    phone: str,
    *,
    inbound_messages: list[str],
    outbound_messages: list[str],
    escalation_summary: str | None = None,
) -> None:
    """Append uma entrada em pacientes/<phone>/conversas.md e push pro repo."""
    if not _is_enabled():
        return

    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    chunks: list[str] = [f"## {timestamp}", ""]
    for m in inbound_messages:
        chunks.append(f"**Inbound**: {m}")
    chunks.append("")
    for m in outbound_messages:
        chunks.append(f"**Outbound**: {m}")
    if escalation_summary:
        chunks.append("")
        chunks.append(f"**Resultado**: {escalation_summary}")
    chunks.append("")
    chunks.append("---")
    chunks.append("")
    entry = "\n".join(chunks)

    async with _lock:
        await _pull_if_stale()
        patient_dir = _vault_path() / "pacientes" / phone
        patient_dir.mkdir(parents=True, exist_ok=True)
        conversas = patient_dir / "conversas.md"
        with conversas.open("a", encoding="utf-8") as f:
            if conversas.stat().st_size == 0:
                f.write(f"# Conversas — {phone}\n\n")
            f.write(entry)

        try:
            _run_git(["add", str(conversas.relative_to(_vault_path()))], cwd=_vault_path())
            _run_git(
                ["commit", "-m", f"bot: conversa com {phone} em {timestamp}"],
                cwd=_vault_path(),
                check=False,  # se nada mudou (ex: paciente sem texto novo), não falha
            )
            _run_git(["push"], cwd=_vault_path())
        except Exception:
            log.exception("falha ao commitar/push da conversa de %s", phone)


async def ensure_stub(phone: str, *, name_hint: str | None = None) -> None:
    """Cria pacientes/<phone>/anamnese.md stub se ainda não existir.

    Marcado com TODO pra Dra. revisar — útil quando a paciente não estava cadastrada
    no vault e o bot capturou só pelo WhatsApp.
    """
    if not _is_enabled():
        return

    async with _lock:
        await _pull_if_stale()
        anamnese = _vault_path() / "pacientes" / phone / "anamnese.md"
        if anamnese.exists():
            return
        anamnese.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        nome_value = name_hint or ""
        content = f'''---
nome: "{nome_value}"
telefone: "{phone}"
data_nascimento:
endereco: ""

dum:
data_provavel_parto:
semanas_atuais:
tipo_gestacao:
risco:
gestacao_planejada:

gestas: 0
partos_normais: 0
cesareas: 0
abortos: 0

alergias: []
condicoes_pre_existentes: []
medicacoes_em_uso: []
grupo_sanguineo: ""

medico_obstetra: "Dra. Leiza"
hospital_referencia: ""
plano_saude: ""

status: ativa
preferencias_atendimento: ""
created_at: {timestamp}
updated_at: {timestamp}
---

# {nome_value or phone}

> ⚠️ **Stub criado automaticamente pelo bot — Dra., por favor revisar e completar.**
>
> Esta paciente entrou em contato pelo WhatsApp sem cadastro prévio no vault. Os dados aqui são placeholder. Edite, preencha o frontmatter e remova esta seção.

## Histórico clínico relevante

## Histórico obstétrico

## Observações pessoais da Dra.

## Plano de acompanhamento
'''
        anamnese.write_text(content, encoding="utf-8")
        try:
            _run_git(["add", str(anamnese.relative_to(_vault_path()))], cwd=_vault_path())
            _run_git(["commit", "-m", f"bot: stub de paciente nova {phone}"], cwd=_vault_path(), check=False)
            _run_git(["push"], cwd=_vault_path())
        except Exception:
            log.exception("falha ao commitar/push do stub de %s", phone)
