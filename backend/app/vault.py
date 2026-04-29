"""Modulo `vault` — historicamente lia anamnese de markdown em vault git.

Apos a remocao do Obsidian (2026-04-28), os dados de paciente vivem no DB
(tabela Patient). Este modulo e' agora um wrapper fino que mantem a API
publica (PatientContext, read_patient, init_vault, append_conversation,
commit_and_push) pra evitar refactor em cascata, mas todas as operacoes
de filesystem/git foram removidas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlmodel import Session, select

from .db import engine

log = logging.getLogger("obstetra.vault")


# =====================================================================
# PatientContext — usado pelo agente principal de triagem
# =====================================================================

@dataclass
class PatientContext:
    """Dados estruturados que o bot le do DB para contextualizar o atendimento.
    Mantem o nome historico 'PatientContext' e o metodo to_prompt_block.
    """
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
    hospital_referencia: str | None = None
    contato_emergencia_nome: str | None = None
    contato_emergencia_telefone: str | None = None
    contato_emergencia_relacao: str | None = None

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
        if self.hospital_referencia:
            lines.append(f"  hospital_referencia: {self.hospital_referencia}")
        if self.contato_emergencia_nome or self.contato_emergencia_telefone:
            lines.append("  contato_emergencia:")
            if self.contato_emergencia_nome:
                lines.append(f"    nome: {self.contato_emergencia_nome}")
            if self.contato_emergencia_relacao:
                lines.append(f"    relacao: {self.contato_emergencia_relacao}")
            if self.contato_emergencia_telefone:
                lines.append(f"    telefone: {self.contato_emergencia_telefone}")
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


# =====================================================================
# Helpers
# =====================================================================

def _split_csv_text(text: str | None) -> list[str] | None:
    """Pega texto livre tipo 'penicilina, dipirona\\namoxicilina' e devolve lista."""
    if not text:
        return None
    parts: list[str] = []
    for chunk in text.replace(",", "\n").splitlines():
        s = chunk.strip()
        if s:
            parts.append(s)
    return parts or None


def _compute_gestational_data(dum: date | None, fallback_weeks: int | None) -> tuple[int | None, str | None]:
    """A partir da DUM, calcula semanas atuais e DPP. Se DUM ausente, usa o fallback."""
    if dum:
        today = date.today()
        delta_days = (today - dum).days
        if delta_days >= 0:
            semanas = delta_days // 7
            dpp = (dum + timedelta(days=280)).isoformat()
            return semanas, dpp
    return fallback_weeks, None


# =====================================================================
# Leitura de paciente — agora le do DB
# =====================================================================

async def read_patient(phone: str) -> PatientContext:
    """Le o registro Patient do DB (filtro por telefone, no tenant default)."""
    from .models import Patient
    with Session(engine) as db:
        # MVP single-tenant — pega o primeiro Patient com esse telefone (tenant_id=1)
        pat = db.exec(
            select(Patient).where(Patient.phone == phone)
        ).first()
        if not pat or not pat.name:
            # Sem cadastro suficiente pra fornecer contexto util
            return PatientContext(found=False)

        semanas, dpp = _compute_gestational_data(pat.dum, pat.gestational_weeks)
        return PatientContext(
            found=True,
            nome=pat.name,
            semanas_atuais=semanas,
            tipo_gestacao=pat.tipo_gestacao,
            risco=pat.risco,
            data_provavel_parto=dpp,
            alergias=_split_csv_text(pat.alergias),
            condicoes_pre_existentes=_split_csv_text(pat.condicoes_pre_existentes),
            medicacoes_em_uso=_split_csv_text(pat.medicacoes_em_uso),
            preferencias_atendimento=pat.preferencias_atendimento,
            historico_clinico=pat.historico_clinico,
            historico_obstetrico=pat.historico_obstetrico,
            observacoes_dra=pat.observacoes_dra,
            hospital_referencia=pat.hospital_referencia,
            contato_emergencia_nome=pat.contato_emergencia_nome,
            contato_emergencia_telefone=pat.contato_emergencia_telefone,
            contato_emergencia_relacao=pat.contato_emergencia_relacao,
        )


# =====================================================================
# No-ops legados — mantidos pra nao quebrar callers
# =====================================================================

async def init_vault() -> None:
    """No-op desde 2026-04-28 (vault git aposentado)."""
    log.debug("init_vault: no-op (vault git removido)")


async def append_conversation(
    phone: str,
    *,
    inbound_messages: list[str],
    outbound_messages: list[str],
    escalation_summary: str | None = None,
) -> None:
    """No-op desde 2026-04-28. Mensagens ja sao persistidas na tabela Message."""
    pass


async def commit_and_push(message: str) -> None:
    """No-op desde 2026-04-28."""
    pass


async def _pull_if_stale() -> None:
    """No-op desde 2026-04-28."""
    pass
