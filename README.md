# Obstetra

Agente conversacional em WhatsApp para pacientes da Dra. Leiza (ginecologia/obstetrícia). Atende 24/7, triando com perguntas de múltipla escolha, e escala para o celular da doutora quando identifica situação que exige contato humano.

**Status:** MVP em desenvolvimento. Escopo inicial: gestantes.

## Stack

- **WhatsApp:** Evolution API v2 (Baileys, não-oficial) — será migrado para WhatsApp Business API oficial no futuro.
- **LLM:** Claude Opus 4.7 via Anthropic SDK, com prompt caching no protocolo clínico.
- **Backend:** Python 3.12 + FastAPI.
- **Dados do app:** SQLite (volume persistente).
- **Dados da Evolution:** PostgreSQL.
- **Deploy:** Railway (dev local via Docker Compose).

## Arquitetura

```
Paciente (WhatsApp)
        |
        v
Evolution API  <--- Postgres
        |
        |  webhook POST /webhook
        v
Backend FastAPI  <--- SQLite
        |
        v
Claude Opus 4.7  ---> (escala) WhatsApp Dra. Leiza
```

A camada `app/providers/` abstrai o canal WhatsApp. Hoje só existe `EvolutionProvider`; quando migrarmos para a API oficial, basta adicionar outro adapter.

## Rodando local

```bash
cp .env.example .env
# edite .env com sua ANTHROPIC_API_KEY e demais segredos
docker compose up -d
```

Depois:

1. Abra `http://localhost:8080/manager` (Evolution API manager) com a `EVOLUTION_API_KEY` do seu `.env`.
2. Crie uma instância com o nome definido em `EVOLUTION_INSTANCE_NAME` (padrão: `obstetra`).
3. Pareie escaneando o QR Code com o WhatsApp que servirá como número do bot.
4. Envie uma mensagem para esse número a partir de outro celular.

O webhook global já vem configurado via env vars para `http://backend:8000/webhook` (rede interna do Docker).

Healthcheck do backend: `http://localhost:8000/health`.

## Deploy no Railway

Ver [docs/railway.md](docs/railway.md) (a ser criado).

## Avisos

- Este agente **não substitui consulta médica**. Toda resposta ao paciente inclui esse disclaimer.
- O atendimento atual é restrito a **gestantes**. Pacientes fora desse escopo são informadas e a Dra. Leiza é notificada.
- Dados clínicos são sensíveis (LGPD). Nunca commitar `.env` ou logs contendo mensagens reais.
